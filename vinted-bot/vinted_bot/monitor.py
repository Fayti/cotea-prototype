"""Boucle de surveillance : une coroutine par recherche, requêtes cadencées par domaine."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from . import filters
from .client import RateLimited, VintedClient
from .config import Config, Search
from .models import Item
from .store import Store

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, cfg: Config, store: Store, notifiers: list, client_factory=None):
        self.cfg = cfg
        self.store = store
        self.notifiers = notifiers
        self._client_factory = client_factory or (
            lambda domain: VintedClient(
                domain,
                proxies=cfg.proxies,
                impersonate=cfg.impersonate,
                min_request_gap=cfg.min_request_gap,
            )
        )
        self.clients: dict[str, VintedClient] = {}
        self._pending: set[asyncio.Task] = set()

    def client(self, domain: str) -> VintedClient:
        if domain not in self.clients:
            self.clients[domain] = self._client_factory(domain)
        return self.clients[domain]

    async def run(self) -> None:
        self.store.prune()
        log.info("Surveillance de %d recherche(s)", len(self.cfg.searches))
        try:
            await asyncio.gather(*(self._watch(s) for s in self.cfg.searches))
        finally:
            for c in self.clients.values():
                await c.close()

    async def poll(self, search: Search) -> list[Item]:
        """Une interrogation. Renvoie les annonces notifiées."""
        warmup = self.store.watermark(search.name) is None
        per_page = self.cfg.warmup_per_page if warmup else self.cfg.per_page
        raw = await self.client(search.domain).search(search.api_params(per_page))
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
                self.store.set_watermark(search.name, 0)
            return []

        # Les ID Vinted sont croissants : une annonce « remontée » garde son vieil ID.
        # Le filigrane élimine donc les remontées et les annonces sponsorisées anciennes.
        watermark = self.store.watermark(search.name) or 0
        fresh = sorted((i for i in items if i.id > watermark), key=lambda i: i.id)
        self.store.set_watermark(search.name, max(i.id for i in items))
        if not warmup and len(fresh) >= self.cfg.per_page:
            log.warning("[%s] page pleine de nouveautés : des annonces ont pu être manquées "
                        "(réduire l'intervalle ou affiner la recherche)", search.name)

        notified = []
        now = time.time()
        for item in fresh:
            if not self.store.mark_seen(search.name, item.id):
                continue
            ok, reason = filters.accept(item, search)
            if not ok:
                log.debug("[%s] rejet %s : %s", search.name, item.id, reason)
                continue
            median = self.store.median_price(search.name, self.cfg.deal_window, self.cfg.deal_min_samples)
            if item.price is not None:
                self.store.add_price(search.name, item.price)
            if warmup:
                continue
            if item.created_ts and now - item.created_ts > self.cfg.max_age:
                continue  # annonce publiée pendant un arrêt prolongé du bot
            deal = filters.evaluate_deal(item.price, median, self.cfg.deal_threshold)
            if search.only_deals and not deal.is_deal:
                continue
            notified.append(item)
            self._dispatch(item, deal, search)
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

    async def _watch(self, search: Search) -> None:
        interval = search.interval or self.cfg.interval
        backoff = 0.0
        while True:
            started = time.monotonic()
            try:
                found = await self.poll(search)
                if found:
                    log.info("[%s] %d nouvelle(s) annonce(s)", search.name, len(found))
                backoff = 0.0
            except RateLimited as e:
                backoff = min(max(backoff * 2, 30.0), 600.0)
                log.warning("[%s] %s — pause de %.0f s", search.name, e, backoff)
                await asyncio.sleep(backoff)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("[%s] erreur : %s", search.name, e)
                await asyncio.sleep(10)
                continue
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, interval - elapsed) + random.uniform(0, self.cfg.jitter))
