"""Orchestration : compte, iPhone, session décomptée, téléportation et trajets."""

import asyncio
import logging
import time
from typing import Optional

from ghostloc_desktop import routes
from ghostloc_desktop.account import Account, AccountError
from ghostloc_desktop.device import IPhoneLink, LinkError

log = logging.getLogger(__name__)

HEARTBEAT_S = 60
MAX_HEARTBEAT_FAILURES = 3

ERRORS = {
    "no_active_subscription": "Aucun abonnement actif. Choisis une formule sur le site.",
    "quota_exhausted": "Tes heures du mois sont épuisées. Ajoute un pack ou passe à l'Illimité.",
    "routes_require_illimite": "Les trajets sont inclus dans l'Illimité.",
    "invalid_credentials": "E-mail ou mot de passe incorrect.",
    "email_not_confirmed": "Confirme d'abord ton e-mail (lien reçu à l'inscription).",
}


class UserError(Exception):
    pass


def _friendly(e: Exception) -> str:
    code = getattr(e, "code", None) or str(e)
    return ERRORS.get(code, str(e))


class Controller:
    def __init__(self, account: Account, link: IPhoneLink):
        self.account = account
        self.link = link
        self.device = {"status": "none", "message": "Recherche de l'iPhone…"}
        self.entitlement: Optional[dict] = None
        self.location: Optional[dict] = None
        self.route: Optional[dict] = None
        self.session_id: Optional[str] = None
        self.notice: Optional[str] = None
        self._lock = asyncio.Lock()
        self._route_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._connecting = False

    # ---- état exposé à l'interface -------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "account": {
                "configured": self.account.configured,
                "email": self.account.email,
                "entitlement": self.entitlement,
            },
            "device": {**self.device, "name": self.link.name, "ios": self.link.ios, "connection": self.link.connection},
            "location": self.location,
            "route": self.route,
            "notice": self.notice,
        }

    def _status(self, status: str, message: str) -> None:
        self.device = {"status": status, "message": message}

    # ---- compte ---------------------------------------------------------------------
    async def login(self, email: str, password: str) -> None:
        try:
            await self.account.login(email, password)
        except AccountError as e:
            raise UserError(_friendly(e)) from e
        await self.refresh_entitlement()

    async def logout(self) -> None:
        await self.go_home()
        self.account.logout()
        self.entitlement = None

    async def refresh_entitlement(self) -> None:
        if not self.account.email:
            self.entitlement = None
            return
        try:
            self.entitlement = await self.account.entitlement()
        except AccountError as e:
            if e.code == "not_authenticated":
                self.entitlement = None
            log.warning("droits indisponibles : %s", e)
        except Exception as e:
            log.warning("droits indisponibles : %s", e)

    # ---- iPhone ---------------------------------------------------------------------
    async def connect(self) -> None:
        if self._connecting:
            return
        self._connecting = True
        try:
            await self.link.connect(self._status)
            self._status("ready", "iPhone prêt.")
        except LinkError as e:
            self._status(e.status, e.message)
        except Exception as e:
            log.exception("connexion iPhone")
            self._status("error", f"Erreur : {e}")
        finally:
            self._connecting = False

    async def reveal_developer_mode(self) -> None:
        try:
            await self.link.reveal_developer_mode()
        except Exception as e:
            raise UserError(f"Impossible d'afficher l'option : {e}") from e

    async def watch_device(self) -> None:
        """Connexion automatique au branchement, nettoyage au débranchement."""
        last_attempt = 0.0
        while True:
            serials = await self.link.attached_serials()
            status = self.device["status"]
            if self.link.ready and self.link.serial not in serials:
                log.info("iPhone débranché")
                await self._drop_session("iPhone débranché : position réelle rétablie.")
                await self.link.close()
                self._status("none", "Branche ton iPhone en USB et déverrouille-le.")
            elif not self.link.ready and status not in ("pairing", "preparing", "denied", "unsupported"):
                # Nouvel essai rapide tant que l'utilisateur agit sur l'iPhone, plus espacé après une erreur.
                delay = 20 if status in ("error", "no_service") else 3
                if time.monotonic() - last_attempt >= delay:
                    last_attempt = time.monotonic()
                    await self.connect()
            await asyncio.sleep(3)

    # ---- session décomptée -------------------------------------------------------------
    async def _ensure_session(self, need_routes: bool = False) -> None:
        if not self.account.email:
            raise UserError("Connecte-toi d'abord à ton compte GhostLoc.")
        if not self.link.ready:
            raise UserError("Ton iPhone n'est pas prêt.")
        if need_routes:
            await self.refresh_entitlement()
            if not (self.entitlement or {}).get("routes"):
                raise UserError(ERRORS["routes_require_illimite"])
        if self.session_id:
            return
        try:
            data = await self.account.session_start(self.link.name or "iPhone")
        except AccountError as e:
            raise UserError(_friendly(e)) from e
        except Exception as e:
            raise UserError("Serveur GhostLoc injoignable. Vérifie ta connexion Internet.") from e
        self.session_id = data["session_id"]
        self.entitlement = data
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        failures = 0
        while self.session_id:
            await asyncio.sleep(HEARTBEAT_S)
            if not self.session_id:
                return
            try:
                data = await self.account.session_heartbeat(self.session_id)
                failures = 0
                self.entitlement = data
                if data.get("stop"):
                    reason = "quota_exhausted" if data.get("active") else "no_active_subscription"
                    await self.go_home(notice=ERRORS[reason])
                    return
            except Exception as e:
                failures += 1
                log.warning("signe de vie refusé (%s/%s) : %s", failures, MAX_HEARTBEAT_FAILURES, e)
                if failures >= MAX_HEARTBEAT_FAILURES:
                    await self.go_home(notice="Connexion Internet perdue : position réelle rétablie.")
                    return

    async def _drop_session(self, notice: Optional[str] = None) -> None:
        if self._route_task:
            self._route_task.cancel()
            self._route_task = None
        self.route = None
        self.location = None
        session_id, self.session_id = self.session_id, None
        if self._heartbeat_task and self._heartbeat_task is not asyncio.current_task():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None
        if session_id:
            try:
                self.entitlement = await self.account.session_stop(session_id)
            except Exception as e:
                log.warning("fin de session non enregistrée : %s", e)
        if notice:
            self.notice = notice

    # ---- actions -------------------------------------------------------------------------
    async def teleport(self, lat: float, lng: float, name: str) -> None:
        async with self._lock:
            self.notice = None
            await self._ensure_session()
            if self._route_task:
                self._route_task.cancel()
                self._route_task = None
                self.route = None
            try:
                await self.link.set(lat, lng)
            except Exception as e:
                await self._fail_device(e)
            self.location = {"lat": lat, "lng": lng, "name": name, "since": time.time()}

    async def start_route(self, lat: float, lng: float, name: str, mode: str, speed_kmh: Optional[float]) -> None:
        async with self._lock:
            self.notice = None
            if not self.location:
                raise UserError("Téléporte-toi d'abord au point de départ.")
            if mode not in routes.MODES:
                raise UserError("Mode de trajet inconnu.")
            await self._ensure_session(need_routes=True)
            speed = max(1.0, min(float(speed_kmh or routes.MODES[mode]), 1200.0))
            start = (self.location["lat"], self.location["lng"])
            if mode == "avion":
                path = routes.great_circle(start, (lat, lng), 200)
            else:
                path = await asyncio.to_thread(routes.road_path, start, (lat, lng), mode)
            total = routes.path_length(path)
            if self._route_task:
                self._route_task.cancel()
            self.route = {
                "to": {"lat": lat, "lng": lng, "name": name}, "mode": mode, "speed_kmh": speed,
                "distance_m": total, "done_m": 0.0, "eta_s": total / (speed / 3.6),
                "path": [[round(p[0], 6), round(p[1], 6)] for p in path[:: max(1, len(path) // 400)]] + [[lat, lng]],
            }
            self._route_task = asyncio.create_task(self._drive(path, speed, name))

    async def _drive(self, path: list, speed: float, name: str) -> None:
        try:
            for (lat, lng), done in routes.walk(path, speed):
                await self.link.set(lat, lng)
                self.location = {**(self.location or {}), "lat": lat, "lng": lng, "name": f"En route vers {name}"}
                if self.route:
                    self.route["done_m"] = done
                    self.route["eta_s"] = max(0.0, (self.route["distance_m"] - done) / (speed / 3.6))
                await asyncio.sleep(1)
            self.location = {**(self.location or {}), "name": name, "since": time.time()}
            self.route = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("trajet interrompu")
            self.route = None
            await self._fail_device(e, raise_error=False)

    async def stop_route(self) -> None:
        if self._route_task:
            self._route_task.cancel()
            self._route_task = None
        self.route = None
        if self.location:
            self.location["name"] = "Position en pause"

    async def go_home(self, notice: Optional[str] = None) -> None:
        if self._route_task:
            self._route_task.cancel()
            self._route_task = None
        try:
            if self.link.ready:
                await self.link.clear()
        except Exception as e:
            log.warning("retour réel : %s", e)
        await self._drop_session(notice)

    async def _fail_device(self, e: Exception, raise_error: bool = True) -> None:
        log.warning("iPhone injoignable : %s", e)
        await self._drop_session("Connexion à l'iPhone perdue.")
        await self.link.close()
        self._status("none", "Connexion perdue. Rebranche ton iPhone.")
        if raise_error:
            raise UserError("Connexion à l'iPhone perdue. Rebranche-le et réessaie.") from e

    async def shutdown(self) -> None:
        await self.go_home()
        await self.link.close()
