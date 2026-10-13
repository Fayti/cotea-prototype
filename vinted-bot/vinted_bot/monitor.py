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
from .tracking import AVAILABLE, DELETED, GONE, SOLD, TRACK_FOR, Tracker, extract_photos, page_status

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
PHOTOS_TTL = 180.0  # au-delà, les photos supplémentaires d'une alerte n'ont plus d'intérêt
PAGE_PAUSE = 900.0  # pause des lectures de pages d'annonce après un blocage
STATUS_WAIT = 120.0  # au-delà, une confirmation de vente passe avant les photos
CONFIRM_TTL = 3 * 3600.0  # vérification d'un départ (veille) abandonnée au-delà
LIKES_EVERY = 15.0        # une relecture de likes toutes les 15 s au plus (≈ 0,07 requête/s)
LIKES_REFRESH = 300.0     # chaque alerte récente relue toutes les 5 min…
LIKES_FOR = 3 * 3600.0    # … pendant ses 3 premières heures
PHOTOS_WAIT = 5.0  # attente maximale des photos avant d'envoyer l'alerte quand même


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
        self.tracker = Tracker()
        self._seq: dict[str, int] = defaultdict(int)
        # Lectures de pages d'annonce en attente : item_id -> {domain, url, reasons, since, tries}
        self._page_queue: dict[int, dict] = {}
        self._page_busy = False
        self._next_page_at = 0.0
        self._page_pause_until = 0.0
        self.on_auto_reserve = None  # rappel (annonce, recherche) pour la réservation automatique
        self.on_confirm = None       # rappel (annonce, état lu sur sa page) pour la veille
        self.page_domains: set[str] = set()  # domaines où lire des pages même sans recherche
        # État par recherche, lu par l'interface.
        self.state: dict[str, dict] = {
            s.key: {"last_ok": None, "last_error": None, "polls": 0, "ready": False}
            for s in cfg.searches
        }

    def client(self, domain: str) -> VintedClient:
        if domain not in self.clients:
            self.clients[domain] = self._client_factory(domain)
        return self.clients[domain]

    def _load_tracked(self) -> None:
        for row in self.store.load_tracked(time.time() - TRACK_FOR):
            if row["status"] in (SOLD, DELETED):
                continue
            t = self.tracker.add(row["item_id"], row["search"], row["url"], row["alerted_ts"], row["status"])
            t.status_ts, t.handles = row["status_ts"], row["handles"]

    async def run(self) -> None:
        self.store.prune()
        self._load_tracked()
        by_domain: dict[str, list[Search]] = defaultdict(list)
        for s in self.cfg.searches:
            by_domain[s.domain].append(s)
        log.info("Surveillance de %d recherche(s)", len(self.cfg.searches))
        try:
            await asyncio.gather(
                *(self._drive(d, ss) for d, ss in by_domain.items()),
                *(self._page_loop(d) for d in self.page_domains - set(by_domain)),
                self._likes_loop(),
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
            elif page := self._pop_page(domain):
                coro = self._fetch_page(domain, lane, inflight, *page)
            else:
                coro = self._poll_safe(next(cycle), lane, inflight)
            task = asyncio.create_task(coro)
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    def _likes_candidate(self, now: float):
        due = [t for t in self.tracker.items.values()
               if t.params is not None and t.price is not None and t.status == AVAILABLE
               and now - t.alerted_ts <= LIKES_FOR and now - t.likes_ts >= LIKES_REFRESH]
        return min(due, key=lambda t: t.likes_ts, default=None)

    async def _likes_loop(self) -> None:
        """Likes en direct des alertes récentes, même sorties de la 1re page de leur recherche."""
        while True:
            await asyncio.sleep(LIKES_EVERY)
            await self.refresh_likes_once()

    async def refresh_likes_once(self) -> None:
        t = self._likes_candidate(time.time())
        if t is not None:
            t.likes_ts = time.time()
            price = f"{t.price:.2f}"
            params = {**t.params, "price_from": price, "price_to": price,
                      "order": "newest_first", "per_page": "20", "page": "1"}
            try:
                raw = await self.client(t.domain).search(params)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("Likes de l'annonce %s : %s", t.item_id, e)
                return
            items = []
            for r in raw:
                try:
                    items.append(Item.from_api(r, t.domain))
                except (KeyError, TypeError, ValueError):
                    pass
            self._update_likes([i for i in items if i.id == t.item_id])

    async def _page_loop(self, domain: str) -> None:
        """Lecteur de pages pour un domaine sans recherche (veille seule) : mêmes règles d'espacement."""
        while True:
            page = self._pop_page(domain)
            if page is None:
                await asyncio.sleep(1.0)
                continue
            lane = await self.client(domain).acquire()
            await self._fetch_page(domain, lane, asyncio.Semaphore(0), *page)

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
        self._seq[search.key] += 1
        seq = self._seq[search.key]
        t0 = time.monotonic()
        raw = await self.client(search.domain).search(search.api_params(per_page), lane)
        self.stats.request_times.append(time.monotonic())
        self.stats.rtts.append(time.monotonic() - t0)
        return self.process(search, raw, warmup, seq)

    def process(self, search: Search, raw_items: list[dict], warmup: bool, seq: int | None = None) -> list[Item]:
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
            if (self.on_auto_reserve and search.auto_reserve_max is not None and item.price is not None
                    and item.price <= search.auto_reserve_max):
                self.on_auto_reserve(item, search)  # avant tout le reste : chaque seconde compte
            t = self.tracker.add(item.id, search.key, item.url)
            t.likes, t.views = item.favourites, item.views
            t.domain, t.params, t.price = search.domain, dict(search.params), item.price
            self.store.save_tracked(t)
            if self.cfg.extra_photos and len(item.photo_urls) < self.cfg.photos_per_alert:
                self._spawn(self._alert_with_photos(item, deal, search), "alerte")
            else:
                self._dispatch(item, deal, search)
            log.info("[%s] %s — %s%s", search.name, item.title, item.url,
                     f" (publiée il y a ~{age:.0f} s)" if age is not None else "")
        if not warmup:
            self._update_likes(items)
        if not warmup and seq is not None and self.cfg.track_status:
            self.tracker.expire()
            visible = [i.id for i in items if not i.promoted]
            promoted = {i.id for i in items if i.promoted}
            for t in self.tracker.observe_page(search.key, seq, visible, promoted):
                self._status_changed(t)
                if t.status == GONE:
                    self._enqueue_page(t.item_id, t.url, search.domain, "status")
        if warmup:
            log.info("[%s] initialisé (%d annonces de référence, aucune alerte)", search.name, len(items))
        elif new_count >= self.cfg.per_page * 0.8:
            log.warning("[%s] presque toute la page est nouvelle : des annonces ont pu être manquées "
                        "(augmenter la priorité ou affiner la recherche)", search.name)
        return notified

    def _spawn(self, coro, what: str) -> None:
        async def guarded():
            try:
                await coro
            except Exception as e:  # une notification ratée ne doit pas tuer le bot
                log.error("Échec de notification (%s) : %s", what, e)
        task = asyncio.get_running_loop().create_task(guarded())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _dispatch(self, item, deal, search) -> None:
        # Les envois ne bloquent jamais le polling.
        for notifier in self.notifiers:
            self._spawn(self._send(notifier, item, deal, search), type(notifier).__name__)

    async def _send(self, notifier, item, deal, search) -> None:
        handle = await notifier.send(item, deal, search)
        t = self.tracker.items.get(item.id)
        if handle is not None and t is not None:
            # Référence du message (ex. Telegram) pour l'enrichir ou le modifier plus tard.
            t.handles[type(notifier).__name__] = handle
            self.store.save_tracked(t)

    async def _alert_with_photos(self, item, deal, search) -> None:
        """Une seule alerte, déjà complète : la page de l'annonce est lue tout de suite (une
        requête, ~1 s) pour joindre les photos suivantes. Si le rythme des pages ne le permet
        pas, l'alerte part sans attendre et l'interface complètera la carte plus tard."""
        photos = await self._photos_now(item, search.domain)
        if len(photos) > len(item.photo_urls):
            item.photo_urls = photos
        elif len(item.photo_urls) < self.cfg.photos_per_alert:
            self._enqueue_page(item.id, item.url, search.domain, "photos")
        self._dispatch(item, deal, search)

    async def _photos_now(self, item, domain: str) -> list[str]:
        now = time.monotonic()
        if self._page_busy or now < self._next_page_at or now < self._page_pause_until:
            return []
        self._page_busy, self._next_page_at = True, now + self.cfg.page_gap
        try:
            async def read():
                client = self.client(domain)
                return await client.get_page(item.url, await client.acquire())
            code, final_url, page = await asyncio.wait_for(read(), PHOTOS_WAIT)
            self.stats.request_times.append(time.monotonic())
            return extract_photos(page, self.cfg.photos_per_alert) if code == 200 else []
        except RateLimited as e:
            self._page_pause_until = time.monotonic() + PAGE_PAUSE
            self.stats.block_times.append(time.monotonic())
            log.warning("Lecture des pages d'annonce suspendue %d min : %s", PAGE_PAUSE // 60, e)
        except Exception as e:
            log.debug("Photos de l'annonce %s indisponibles : %s", item.id, e)
        finally:
            self._page_busy = False
        return []

    def _update_likes(self, items) -> None:
        """Favoris et vues des annonces déjà signalées, relevés gratuitement à chaque passage."""
        for item in items:
            t = self.tracker.items.get(item.id)
            if t is None or (t.likes, t.views) == (item.favourites, item.views):
                continue
            t.likes, t.views = item.favourites, item.views
            for notifier in self.notifiers:
                if hasattr(notifier, "likes"):
                    self._spawn(notifier.likes(t), f"{type(notifier).__name__}.likes")

    def _status_changed(self, t) -> None:
        self.store.save_tracked(t)
        log.info("Annonce %s : %s", t.item_id, t.status)
        for notifier in self.notifiers:
            if hasattr(notifier, "status"):
                name = type(notifier).__name__
                self._spawn(notifier.status(t, t.handles.get(name)), f"{name}.status")

    # --- pages d'annonce : photos supplémentaires et confirmation du motif ---------------

    def _enqueue_page(self, item_id: int, url: str, domain: str, reason: str) -> None:
        entry = self._page_queue.setdefault(
            item_id, {"domain": domain, "url": url, "reasons": set(), "since": time.monotonic(), "tries": 0})
        entry["reasons"].add(reason)

    def _pop_page(self, domain: str):
        """Prochaine page à lire sur ce domaine, ou None. Une seule à la fois, espacées."""
        now = time.monotonic()
        if self._page_busy or now < self._next_page_at or now < self._page_pause_until:
            return None
        for iid in list(self._page_queue):
            entry = self._page_queue[iid]
            if "photos" in entry["reasons"] and now - entry["since"] > PHOTOS_TTL:
                entry["reasons"].discard("photos")
                log.info("Photos de l'annonce %s abandonnées (file d'attente trop longue)", iid)
            if "confirm" in entry["reasons"] and now - entry["since"] > CONFIRM_TTL:
                entry["reasons"].discard("confirm")
            if not entry["reasons"]:
                del self._page_queue[iid]
        # Photos d'abord, de la plus récente alerte à la plus ancienne : c'est sur la dernière
        # annonce arrivée qu'il faut décider vite. Confirmations de vente ensuite, dans l'ordre,
        # sauf celles qui attendent depuis plus de STATUS_WAIT : elles passent devant.
        # Vérifications de la veille en dernier : elles servent aux statistiques, pas à acheter.
        def rank(e):
            waited = now - e["since"]
            if "status" in e["reasons"] and waited > STATUS_WAIT:
                return (0, e["since"])
            if "photos" in e["reasons"]:
                return (1, -e["since"])
            if "status" in e["reasons"]:
                return (2, e["since"])
            return (3, e["since"])
        candidates = [(*rank(e), iid) for iid, e in self._page_queue.items() if e["domain"] == domain]
        if not candidates:
            return None
        _, _, iid = min(candidates)
        entry = self._page_queue.pop(iid)
        self._page_busy = True
        self._next_page_at = now + self.cfg.page_gap
        return iid, entry

    async def _fetch_page(self, domain: str, lane, inflight: asyncio.Semaphore, item_id: int, entry: dict) -> None:
        try:
            code, final_url, page = await self.client(domain).get_page(entry["url"], lane)
            self.stats.request_times.append(time.monotonic())
            if "confirm" in entry["reasons"] and self.on_confirm:
                verdict = page_status(code, final_url, page)
                if verdict is None and entry["tries"] < 1:   # page illisible : un nouvel essai
                    entry["tries"] += 1
                    self._page_queue.setdefault(item_id, {**entry, "reasons": {"confirm"}})
                else:
                    self.on_confirm(item_id, verdict)
            t = self.tracker.items.get(item_id)
            if "photos" in entry["reasons"]:
                photos = extract_photos(page, self.cfg.photos_per_alert)
                if len(photos) >= 2:
                    for notifier in self.notifiers:
                        if hasattr(notifier, "photos"):
                            name = type(notifier).__name__
                            handle = t.handles.get(name) if t else None
                            self._spawn(notifier.photos(item_id, photos, handle), f"{name}.photos")
            status = page_status(code, final_url, page) if self.cfg.track_status else None
            if t is not None and status == AVAILABLE and "status" not in entry["reasons"]:
                status = None  # lecture pour les photos, peut-être antérieure à la disparition
            if t is not None and status is not None and status != t.status:
                if status == AVAILABLE:
                    # Encore en vente mais sortie de la recherche (prix modifié…) : on cesse de
                    # la juger sur sa présence jusqu'à ce qu'elle réapparaisse.
                    t.anchors, t.missing = (), 0
                t.status, t.status_ts = status, time.time()
                self._status_changed(t)
            elif t is not None and status is None and "status" in entry["reasons"]:
                log.info("Annonce %s : page illisible, motif non confirmé (reste « plus disponible »)", item_id)
        except RateLimited as e:
            self._page_pause_until = time.monotonic() + PAGE_PAUSE
            self.stats.block_times.append(time.monotonic())
            log.warning("Lecture des pages d'annonce suspendue %d min : %s", PAGE_PAUSE // 60, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            entry["tries"] += 1
            if entry["tries"] < 2:
                self._page_queue.setdefault(item_id, entry)
            log.debug("Page de l'annonce %s illisible : %s", item_id, e)
        finally:
            self._page_busy = False
            inflight.release()

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
