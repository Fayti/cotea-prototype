"""Ordonnanceur : envoie les requêtes au rythme maximal toléré, sans attendre les réponses.

Une seule boucle par domaine passe d'une recherche à l'autre (pondéré par `weight`) dès
qu'une voie (IP) est libre. Les requêtes se chevauchent : le temps de réponse de Vinted ne
s'ajoute donc pas à l'intervalle entre deux passages sur une même recherche.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import OrderedDict, defaultdict, deque

from . import filters
from .client import RateLimited, VintedClient
from .config import Config, Search
from .models import Item
from .store import Store

log = logging.getLogger(__name__)


def weighted_cycle(searches: list[Search]):
    """Round-robin pondéré lissé : poids 3 = 3 passages pour 1, répartis régulièrement."""
    current = [0] * len(searches)
    total = sum(s.weight for s in searches)
    while True:
        for i, s in enumerate(searches):
            current[i] += s.weight
        best = max(range(len(searches)), key=current.__getitem__)
        current[best] -= total
        yield searches[best]


CLOCK_EVERY = 30.0  # secondes entre deux relevés du numéro d'annonce le plus récent


class IdClock:
    """Estime l'âge d'une annonce à partir de son numéro.

    Vinted ne renvoie plus de date de publication (septembre 2026). Les numéros sont attribués
    à la création de l'annonce, au rythme d'environ 15 000 par minute : en relevant
    régulièrement le numéro le plus récent du catalogue, on convertit un écart de numéros
    en secondes. Précision : quelques secondes, plus le temps passé par le vendeur à remplir
    son annonce après l'envoi des photos.
    """

    DEFAULT_RATE = 250.0  # numéros par seconde, mesuré en octobre 2026

    def __init__(self):
        self.samples: deque[tuple[float, int]] = deque(maxlen=40)

    @property
    def ready(self) -> bool:
        return bool(self.samples)

    def observe(self, ts: float, frontier: int) -> None:
        if self.samples and frontier < self.samples[-1][1]:
            frontier = self.samples[-1][1]
        self.samples.append((ts, frontier))

    def rate(self) -> float:
        if len(self.samples) >= 2:
            (t0, i0), (t1, i1) = self.samples[0], self.samples[-1]
            if t1 - t0 >= 60 and i1 > i0:
                return (i1 - i0) / (t1 - t0)
        return self.DEFAULT_RATE

    def age(self, item_id: int, now: float | None = None) -> float | None:
        if not self.samples:
            return None
        now = now or time.time()
        t, frontier = self.samples[-1]
        rate = self.rate()
        return max(0.0, (frontier + (now - t) * rate - item_id) / rate)


class Stats:
    """Statistiques glissantes (dernière minute pour le débit, 100 derniers points sinon)."""

    WINDOW = 60.0

    def __init__(self):
        self.started = time.monotonic()
        self.request_times: deque[float] = deque(maxlen=5000)
        self.block_times: deque[float] = deque(maxlen=500)
        self.error_times: deque[float] = deque(maxlen=500)
        self.rtts: deque[float] = deque(maxlen=100)
        self.ages: deque[float] = deque(maxlen=100)
        self.alerts = 0

    def _recent(self, times) -> int:
        now = time.monotonic()
        return sum(1 for t in times if now - t <= self.WINDOW)

    def rate(self) -> float:
        span = min(self.WINDOW, max(1.0, time.monotonic() - self.started))
        return self._recent(self.request_times) / span

    def snapshot(self) -> dict:
        return {
            "rate": round(self.rate(), 2),
            "rtt_ms": round(statistics.median(self.rtts) * 1000) if self.rtts else None,
            "blocked": self._recent(self.block_times),
            "errors": self._recent(self.error_times),
            "alerts": self.alerts,
            "photo_age_s": round(statistics.median(self.ages)) if self.ages else None,
        }


class Monitor:
    def __init__(self, cfg: Config, store: Store, notifiers: list, client_factory=None):
        self.cfg = cfg
        self.store = store
        self.notifiers = notifiers
        self._client_factory = client_factory or (
            lambda domain: VintedClient(
                domain,
                proxies=cfg.proxies,
                use_direct=cfg.use_direct,
                impersonate=cfg.impersonate,
                request_gap=cfg.request_gap,
            )
        )
        self.clients: dict[str, VintedClient] = {}
        self._pending: set[asyncio.Task] = set()
        self._alerted: OrderedDict[int, None] = OrderedDict()  # une annonce = une seule alerte
        self.clock = IdClock()
        self.stats = Stats()
        # État par recherche, lu par l'interface.
        self.state: dict[str, dict] = {
            s.key: {"last_ok": None, "last_error": None, "polls": 0, "ready": False}
            for s in cfg.searches
        }

    def client(self, domain: str) -> VintedClient:
        if domain not in self.clients:
            self.clients[domain] = self._client_factory(domain)
        return self.clients[domain]

    async def run(self) -> None:
        self.store.prune()
        by_domain: dict[str, list[Search]] = defaultdict(list)
        for s in self.cfg.searches:
            by_domain[s.domain].append(s)
        log.info("Surveillance de %d recherche(s)", len(self.cfg.searches))
        try:
            await asyncio.gather(
                *(self._drive(d, ss) for d, ss in by_domain.items()),
                self._report(),
            )
        finally:
            for t in list(self._pending):
                t.cancel()
            for c in self.clients.values():
                await c.close()

    async def _drive(self, domain: str, searches: list[Search]) -> None:
        client = self.client(domain)
        inflight = asyncio.Semaphore(self.cfg.max_inflight * len(client.lanes))
        cycle = weighted_cycle(searches)
        next_clock = 0.0
        while True:
            await inflight.acquire()
            lane = await client.acquire()
            if time.monotonic() >= next_clock:
                next_clock = time.monotonic() + CLOCK_EVERY
                coro = self._tick_clock(domain, lane, inflight)
            else:
                coro = self._poll_safe(next(cycle), lane, inflight)
            task = asyncio.create_task(coro)
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _tick_clock(self, domain: str, lane, inflight: asyncio.Semaphore) -> None:
        """Relève le numéro d'annonce le plus récent de tout le catalogue (sert à dater)."""
        try:
            raw = await self.client(domain).search({"order": "newest_first", "per_page": "10", "page": "1"}, lane)
            ids = [Item.from_api(r, domain) for r in raw]
            ids = [i.id for i in ids if not i.promoted]
            if ids:
                self.clock.observe(time.time(), max(ids))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("Relevé d'horloge impossible : %s", e)
        finally:
            inflight.release()

    async def _poll_safe(self, search: Search, lane, inflight: asyncio.Semaphore) -> None:
        state = self.state.setdefault(search.key, {"last_ok": None, "last_error": None, "polls": 0, "ready": False})
        try:
            await self.poll(search, lane)
            state.update(last_ok=time.time(), polls=state["polls"] + 1, ready=True, last_error=None)
        except RateLimited as e:
            self.stats.block_times.append(time.monotonic())
            state["last_error"] = f"Bloqué par Vinted, pause automatique ({e})"
            log.warning("[%s] %s", search.name, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.stats.error_times.append(time.monotonic())
            state["last_error"] = f"{type(e).__name__}: {e}"
            log.error("[%s] erreur : %s: %s", search.name, type(e).__name__, e)
        finally:
            inflight.release()

    async def poll(self, search: Search, lane=None) -> list[Item]:
        """Une interrogation. Renvoie les annonces notifiées."""
        warmup = self.store.watermark(search.key) is None
        per_page = self.cfg.warmup_per_page if warmup else self.cfg.per_page
        t0 = time.monotonic()
        raw = await self.client(search.domain).search(search.api_params(per_page), lane)
        self.stats.request_times.append(time.monotonic())
        self.stats.rtts.append(time.monotonic() - t0)
        return self.process(search, raw, warmup)

    def process(self, search: Search, raw_items: list[dict], warmup: bool) -> list[Item]:
        items = []
        for raw in raw_items:
            try:
                items.append(Item.from_api(raw, search.domain))
            except (KeyError, TypeError, ValueError):
                log.debug("Annonce illisible ignorée : %r", raw)
        if not items:
            if warmup:
                self.store.set_watermark(search.key, 0)
            return []

        # Nouvelle annonce = jamais vue dans cette recherche ET récente.
        # Les numéros ne suivent pas l'ordre d'apparition (mesuré : 43 % des nouveautés ont un
        # numéro inférieur au plus grand déjà vu, Vinted indexant avec un léger décalage). Un
        # simple « numéro > dernier vu » en raterait donc près de la moitié. La récence écarte
        # les annonces anciennes remontées, sponsorisées ou révélées par une vente au-dessus.
        newest = self.store.watermark(search.key) or 0
        floor = newest - self.cfg.max_age * self.clock.rate()
        self.store.set_watermark(search.key, max(i.id for i in items))

        notified, new_count = [], 0
        now = time.time()
        for item in sorted(items, key=lambda i: i.id):
            if item.id <= floor or not self.store.mark_seen(search.key, item.id):
                continue
            new_count += 1
            if item.created_ts is None and self.clock.ready:
                item.created_ts, item.age_estimated = now - self.clock.age(item.id, now), True
            age = now - item.created_ts if item.created_ts else None
            ok, reason = filters.accept(item, search)
            if not ok:
                log.debug("[%s] rejet %s : %s", search.name, item.id, reason)
                continue
            median = self.store.median_price(search.key, self.cfg.deal_window, self.cfg.deal_min_samples)
            if item.price is not None:
                self.store.add_price(search.key, item.price)
            if warmup:
                continue
            if age is not None and age > self.cfg.max_age:
                continue  # ancienne annonce remontée, ou publiée pendant un arrêt du bot
            deal = filters.evaluate_deal(item.price, median, self.cfg.deal_threshold)
            if search.only_deals and not deal.is_deal:
                continue
            if item.id in self._alerted:
                continue  # déjà signalée par une autre recherche
            self._alerted[item.id] = None
            if len(self._alerted) > 10000:
                self._alerted.popitem(last=False)
            notified.append(item)
            self.stats.alerts += 1
            if age is not None:
                self.stats.ages.append(age)
            self._dispatch(item, deal, search)
            log.info("[%s] %s — %s%s", search.name, item.title, item.url,
                     f" (publiée il y a ~{age:.0f} s)" if age is not None else "")
        if warmup:
            log.info("[%s] initialisé (%d annonces de référence, aucune alerte)", search.name, len(items))
        elif new_count >= self.cfg.per_page * 0.8:
            log.warning("[%s] presque toute la page est nouvelle : des annonces ont pu être manquées "
                        "(augmenter la priorité ou affiner la recherche)", search.name)
        return notified

    def _dispatch(self, item, deal, search) -> None:
        # Les envois ne bloquent jamais le polling.
        for notifier in self.notifiers:
            task = asyncio.get_running_loop().create_task(self._send(notifier, item, deal, search))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    @staticmethod
    async def _send(notifier, item, deal, search) -> None:
        try:
            await notifier.send(item, deal, search)
        except Exception as e:  # une alerte ratée ne doit pas tuer le bot
            log.error("Échec de notification %s : %s", type(notifier).__name__, e)

    def cycle_seconds(self, search: Search) -> float:
        """Temps moyen entre deux passages sur une recherche, au rythme actuel."""
        same = [s for s in self.cfg.searches if s.domain == search.domain]
        share = search.weight / sum(s.weight for s in same)
        return 1 / (self.client(search.domain).requests_per_second * share)

    def summary(self) -> str:
        snap = self.stats.snapshot()
        parts = [f"{snap['rate']:.2f} req/s"]
        if snap["rtt_ms"] is not None:
            parts.append(f"réponse Vinted médiane {snap['rtt_ms']} ms")
        cycles = ", ".join(f"{s.name} {self.cycle_seconds(s):.1f} s" for s in self.cfg.searches)
        parts.append(f"passage toutes les : {cycles}")
        if snap["blocked"] or snap["errors"]:
            parts.append(f"{snap['blocked']} blocage(s), {snap['errors']} erreur(s) en 1 min")
        if snap["photo_age_s"] is not None:
            parts.append(f"{snap['alerts']} alerte(s), publiées il y a ~{snap['photo_age_s']} s (médiane)")
        return " · ".join(parts)

    def status(self) -> dict:
        """Instantané complet pour l'interface."""
        now = time.monotonic()
        lanes = []
        for client in self.clients.values():
            for lane in client.lanes:
                lanes.append({
                    "label": lane.label,
                    "gap": round(lane.gap, 2),
                    "paused_s": max(0, round(lane.cooldown_until - now)),
                })
        return {
            **self.stats.snapshot(),
            "lanes": lanes,
            "searches": {
                s.key: {**self.state.get(s.key, {}), "cycle_s": round(self.cycle_seconds(s), 1)}
                for s in self.cfg.searches
            },
        }

    async def _report(self) -> None:
        if self.cfg.stats_every <= 0:
            return
        while True:
            await asyncio.sleep(self.cfg.stats_every)
            log.info("Stats : %s", self.summary())
