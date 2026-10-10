"""Client HTTP pour l'API catalogue de Vinted.

Vinted n'offre aucun flux temps réel : la seule source est l'endpoint catalogue trié
par « newest_first ». La vitesse de détection est donc bornée par le nombre de requêtes
qu'une IP peut envoyer sans être bloquée. Chaque IP (connexion directe ou proxy) est une
« voie » avec sa propre session, ses cookies et son propre rythme, ajusté automatiquement :
plus lent après un blocage, puis retour progressif vers le rythme plancher configuré.

L'API exige le cookie de session `access_token_web`, obtenu en chargeant la page d'accueil.
curl_cffi imite l'empreinte TLS/HTTP2 d'un vrai Chrome, ce qui évite la majorité des
blocages anti-bot qui visent `requests`/`httpx`.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)

MAX_GAP = 10.0        # rythme le plus lent toléré par voie (s entre deux requêtes)
RECOVERY = 0.995      # à chaque succès, l'écart se rapproche du plancher
SLOWDOWN = 1.5        # à chaque blocage, l'écart est multiplié d'autant
JITTER = 0.15         # ±15 % : un rythme parfaitement régulier est une signature de bot


class VintedError(Exception):
    pass


class RateLimited(VintedError):
    """403/429 : Vinted nous freine ou nous bloque."""


class Lane:
    def __init__(self, proxy: str | None, gap: float):
        self.proxy = proxy
        self.base_gap = gap
        self.gap = gap
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.strikes = 0
        self.session: AsyncSession | None = None
        self.lock = asyncio.Lock()

    @property
    def label(self) -> str:
        return self.proxy.rsplit("@", 1)[-1] if self.proxy else "direct"

    def succeeded(self) -> None:
        self.strikes = 0
        self.gap = max(self.base_gap, self.gap * RECOVERY)

    def blocked(self) -> float:
        now = time.monotonic()
        if now < self.cooldown_until:
            # Autre requête en vol touchée par le même blocage : pas de double peine.
            return self.cooldown_until - now
        self.strikes += 1
        self.gap = min(MAX_GAP, self.gap * SLOWDOWN)
        cooldown = min(30.0 * 2 ** (self.strikes - 1), 600.0)
        self.cooldown_until = now + cooldown
        self.next_at = max(self.next_at, self.cooldown_until)
        return cooldown

    def delay(self, seconds: float) -> None:
        self.next_at = max(self.next_at, time.monotonic() + seconds)


class VintedClient:
    def __init__(
        self,
        domain: str,
        proxies: list[str] | None = None,
        use_direct: bool = True,
        impersonate: str = "chrome",
        request_gap: float = 1.0,
        timeout: float = 10.0,
        base_url: str | None = None,
    ):
        self.domain = domain
        self.base = base_url or f"https://www.{domain}"
        self._impersonate = impersonate
        self._timeout = timeout
        lanes = ([None] if use_direct or not proxies else []) + list(proxies or [])
        self.lanes = [Lane(p, request_gap) for p in lanes]
        self._acquire_lock = asyncio.Lock()

    @property
    def requests_per_second(self) -> float:
        """Débit actuel, toutes voies confondues."""
        return sum(1 / lane.gap for lane in self.lanes)

    async def acquire(self) -> Lane:
        """Attend la première voie libre et lui réserve le créneau suivant."""
        async with self._acquire_lock:
            lane = min(self.lanes, key=lambda l: l.next_at)
            wait = lane.next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            now = time.monotonic()
            lane.next_at = now + lane.gap * random.uniform(1 - JITTER, 1 + JITTER)
            return lane

    async def _session(self, lane: Lane) -> AsyncSession:
        async with lane.lock:
            if lane.session is not None:
                return lane.session
            session = AsyncSession(
                impersonate=self._impersonate,
                proxy=lane.proxy,
                timeout=self._timeout,
                headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
            )
            resp = await session.get(self.base + "/")
            if resp.status_code in (403, 429):
                await session.close()
                raise RateLimited(f"page d'accueil {self.domain} via {lane.label} : HTTP {resp.status_code}")
            if resp.status_code >= 400:
                await session.close()
                raise VintedError(f"page d'accueil {self.domain} via {lane.label} : HTTP {resp.status_code}")
            if "access_token_web" not in session.cookies:
                log.warning("Cookie access_token_web absent après chargement de %s", self.base)
            log.info("Session Vinted ouverte sur %s via %s", self.domain, lane.label)
            lane.session = session
            return session

    async def _reset(self, lane: Lane) -> None:
        async with lane.lock:
            if lane.session is not None:
                await lane.session.close()
                lane.session = None

    async def search(self, params: dict[str, str], lane: Lane | None = None) -> list[dict]:
        lane = lane or await self.acquire()
        try:
            for attempt in range(2):
                session = await self._session(lane)
                resp = await session.get(
                    f"{self.base}/api/v2/catalog/items",
                    params=params,
                    headers={"Accept": "application/json, text/plain, */*", "Referer": self.base + "/catalog"},
                )
                if resp.status_code == 401 and attempt == 0:
                    log.info("Jeton expiré sur %s (%s), renouvellement", self.domain, lane.label)
                    await self._reset(lane)
                    continue
                if resp.status_code in (403, 429):
                    raise RateLimited(f"API {self.domain} via {lane.label} : HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise VintedError(f"API {self.domain} via {lane.label} : HTTP {resp.status_code}")
                try:
                    data = resp.json()
                except ValueError as e:
                    raise VintedError(f"réponse non JSON ({resp.text[:120]!r})") from e
                lane.succeeded()
                return data.get("items") or []
            raise VintedError("authentification impossible après renouvellement du jeton")
        except RateLimited as e:
            cooldown = lane.blocked()
            await self._reset(lane)  # cookies neufs au prochain essai
            raise RateLimited(f"{e} — voie en pause {cooldown:.0f} s, écart porté à {lane.gap:.2f} s") from e
        except VintedError:
            raise
        except Exception:
            lane.delay(5.0)  # erreur réseau : on laisse respirer cette voie
            raise

    async def close(self) -> None:
        for lane in self.lanes:
            await self._reset(lane)
