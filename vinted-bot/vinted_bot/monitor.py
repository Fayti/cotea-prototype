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
        for search in weighted_cycle(searches):
            await inflight.acquire()
            lane = await client.acquire()
            task = asyncio.create_task(self._poll_safe(search, lane, inflight))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

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

        # Les ID Vinted sont croissants : une annonce « remontée » garde son vieil ID.
        # Le filigrane élimine donc les remontées et les annonces sponsorisées anciennes.
        watermark = self.store.watermark(search.key) or 0
        fresh = sorted((i for i in items if i.id > watermark), key=lambda i: i.id)
        self.store.set_watermark(search.key, max(i.id for i in items))
        if not warmup and len(fresh) >= self.cfg.per_page:
            log.warning("[%s] page pleine de nouveautés : des annonces ont pu être manquées "
                        "(augmenter per_page ou weight, ou affiner la recherche)", search.name)

        notified = []
        now = time.time()
        for item in fresh:
            if not self.store.mark_seen(search.key, item.id):
                continue
            ok, reason = filters.accept(item, search)
            if not ok:
                log.debug("[%s] rejet %s : %s", search.name, item.id, reason)
                continue
            median = self.store.median_price(search.key, self.cfg.deal_window, self.cfg.deal_min_samples)
            if item.price is not None:
                self.store.add_price(search.key, item.price)
            if warmup:
                continue
            age = now - item.created_ts if item.created_ts else None
            if age is not None and age > self.cfg.max_age:
                continue  # annonce publiée pendant un arrêt prolongé du bot
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
                     f" (photo de il y a {age:.0f} s)" if age is not None else "")
        if warmup:
            log.info("[%s] initialisé (%d annonces de référence, aucune alerte)", search.name, len(items))
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
            parts.append(f"{snap['alerts']} alerte(s), âge photo médian {snap['photo_age_s']} s")
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
