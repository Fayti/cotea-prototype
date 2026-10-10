"""Client HTTP pour l'API catalogue de Vinted.

Vinted n'offre aucun flux temps réel : la seule source est l'endpoint catalogue trié
par « newest_first ». La latence de détection = intervalle de polling + durée de la requête.
L'API exige le cookie de session `access_token_web`, obtenu en chargeant la page d'accueil.
curl_cffi imite l'empreinte TLS/HTTP2 d'un vrai Chrome, ce qui évite la majorité des
blocages anti-bot qui visent `requests`/`httpx`.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)


class VintedError(Exception):
    pass


class RateLimited(VintedError):
    """403/429 : Vinted nous freine ou nous bloque."""


class VintedClient:
    def __init__(
        self,
        domain: str,
        proxies: list[str] | None = None,
        impersonate: str = "chrome",
        min_request_gap: float = 1.0,
        timeout: float = 10.0,
        base_url: str | None = None,
    ):
        self.domain = domain
        self.base = base_url or f"https://www.{domain}"
        self._proxies = itertools.cycle(proxies) if proxies else None
        self._impersonate = impersonate
        self._timeout = timeout
        self._min_gap = min_request_gap
        self._session: AsyncSession | None = None
        self._session_lock = asyncio.Lock()
        self._gap_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _throttle(self) -> None:
        """Écart minimal entre deux requêtes vers ce domaine, toutes recherches confondues."""
        async with self._gap_lock:
            wait = self._last_request + self._min_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _ensure_session(self) -> AsyncSession:
        async with self._session_lock:
            if self._session is not None:
                return self._session
            proxy = next(self._proxies) if self._proxies else None
            session = AsyncSession(
                impersonate=self._impersonate,
                proxy=proxy,
                timeout=self._timeout,
                headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
            )
            await self._throttle()
            resp = await session.get(self.base + "/")
            if resp.status_code in (403, 429):
                await session.close()
                raise RateLimited(f"Page d'accueil {self.domain} : HTTP {resp.status_code}")
            if resp.status_code >= 400:
                await session.close()
                raise VintedError(f"Page d'accueil {self.domain} : HTTP {resp.status_code}")
            if "access_token_web" not in session.cookies:
                log.warning("Cookie access_token_web absent après chargement de %s", self.base)
            log.info("Session Vinted ouverte sur %s%s", self.domain, f" via {proxy}" if proxy else "")
            self._session = session
            return session

    async def reset(self) -> None:
        async with self._session_lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def search(self, params: dict[str, str]) -> list[dict]:
        for attempt in range(2):
            session = await self._ensure_session()
            await self._throttle()
            resp = await session.get(
                f"{self.base}/api/v2/catalog/items",
                params=params,
                headers={"Accept": "application/json, text/plain, */*", "Referer": self.base + "/catalog"},
            )
            if resp.status_code == 401 and attempt == 0:
                # Jeton expiré : on recharge les cookies et on réessaie une fois.
                log.info("Jeton expiré sur %s, renouvellement", self.domain)
                await self.reset()
                continue
            if resp.status_code in (403, 429):
                await self.reset()  # nouvelle session (et proxy suivant) au prochain essai
                raise RateLimited(f"API {self.domain} : HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise VintedError(f"API {self.domain} : HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as e:
                raise VintedError(f"Réponse non JSON ({resp.text[:120]!r})") from e
            return data.get("items") or []
        raise VintedError("Authentification impossible après renouvellement du jeton")

    async def close(self) -> None:
        await self.reset()
