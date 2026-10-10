"""Compte GhostLoc : connexion Supabase et décompte des heures côté serveur."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

import requests


class AccountError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class Account:
    def __init__(self, url: str, anon_key: str, store: Path):
        self.url = url
        self.anon_key = anon_key
        self.store = store
        self.session: Optional[dict] = None
        if store.exists():
            try:
                self.session = json.loads(store.read_text(encoding="utf-8"))
            except ValueError:
                self.session = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key and "YOUR-PROJECT" not in self.url)

    @property
    def email(self) -> Optional[str]:
        return (self.session or {}).get("email")

    def _save(self) -> None:
        if self.session is None:
            self.store.unlink(missing_ok=True)
        else:
            self.store.write_text(json.dumps(self.session), encoding="utf-8")

    def _auth(self, grant: str, payload: dict) -> None:
        resp = requests.post(
            f"{self.url}/auth/v1/token?grant_type={grant}",
            headers={"apikey": self.anon_key, "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            code = data.get("error_code") or data.get("error") or "auth_failed"
            raise AccountError(code, data.get("msg") or data.get("error_description") or code)
        self.session = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": int(data.get("expires_at") or time.time() + int(data.get("expires_in", 3600))),
            "email": data["user"]["email"],
        }
        self._save()

    def _token(self) -> str:
        if not self.session:
            raise AccountError("not_authenticated", "Connecte-toi à ton compte GhostLoc.")
        if self.session["expires_at"] - time.time() < 120:
            try:
                self._auth("refresh_token", {"refresh_token": self.session["refresh_token"]})
            except AccountError as e:
                if e.code in ("refresh_token_not_found", "refresh_token_already_used", "invalid_grant", "session_not_found"):
                    self.session = None
                    self._save()
                    raise AccountError("not_authenticated", "Session expirée, reconnecte-toi.") from e
                raise
        return self.session["access_token"]

    def _rpc(self, name: str, args: dict) -> Any:
        resp = requests.post(
            f"{self.url}/rest/v1/rpc/{name}",
            headers={
                "apikey": self.anon_key,
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            json=args,
            timeout=15,
        )
        data = resp.json() if resp.content else None
        if resp.status_code >= 400:
            message = (data or {}).get("message", f"HTTP {resp.status_code}")
            if resp.status_code == 401:
                raise AccountError("not_authenticated", "Session expirée, reconnecte-toi.")
            raise AccountError(message, message)
        return data

    # API asynchrone : les appels réseau bloquants partent dans un thread.
    async def login(self, email: str, password: str) -> None:
        await asyncio.to_thread(self._auth, "password", {"email": email, "password": password})

    def logout(self) -> None:
        self.session = None
        self._save()

    async def entitlement(self) -> dict:
        return await asyncio.to_thread(self._rpc, "gl_entitlement", {})

    async def session_start(self, device: str) -> dict:
        return await asyncio.to_thread(self._rpc, "gl_session_start", {"p_device": device})

    async def session_heartbeat(self, session_id: str) -> dict:
        return await asyncio.to_thread(self._rpc, "gl_session_heartbeat", {"p_session": session_id})

    async def session_stop(self, session_id: str) -> dict:
        return await asyncio.to_thread(self._rpc, "gl_session_stop", {"p_session": session_id})
