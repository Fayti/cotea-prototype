"""Pilotage de la fenêtre Vinted (voir window.py) depuis le bot.

La fenêtre tourne dans un processus séparé. Les échanges passent par des fils et non par
asyncio.subprocess, qui n'existe pas avec la boucle « Selector » utilisée sous Windows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

RESERVE_WAIT = 40.0      # au-delà, la réservation est considérée comme ratée
DRAFT_WAIT = 150.0       # envoi des photos + brouillon (ou formulaire pré-rempli)
AUTO_MAX_PER_HOUR = 10   # garde-fou : réservations automatiques par heure au plus


def window_command(data_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):  # exécutable PyInstaller
        return [sys.executable, "vinted-window", "--data-dir", str(data_dir)]
    return [sys.executable, "-m", "vinted_bot", "vinted-window", "--data-dir", str(data_dir)]


class VintedWindow:
    def __init__(self, data_dir: Path, on_change=None, command: list[str] | None = None):
        self.data_dir = data_dir
        self.on_change = on_change or (lambda: None)
        self.command = command or window_command(data_dir)
        self.proc: subprocess.Popen | None = None
        self.user: str | None = None
        self.busy_until = 0.0
        self.auto_times: list[float] = []
        self._waiting: dict[str, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def state(self) -> dict:
        return {"open": self.is_open, "user": self.user if self.is_open else None}

    async def open(self) -> None:
        """Ouvre la fenêtre, ou la ramène devant si elle est déjà ouverte."""
        if self.is_open:
            self._send({"cmd": "show"})
            return
        self._loop = asyncio.get_running_loop()
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, encoding="utf-8", bufsize=1)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        log.info("Fenêtre Vinted ouverte : connecte-toi à ton compte dedans (une seule fois)")
        self.on_change()

    def _send(self, cmd: dict) -> None:
        if not self.is_open:
            return
        try:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # messages de la bibliothèque de fenêtres
            self._loop.call_soon_threadsafe(self._on_event, msg)
        self._loop.call_soon_threadsafe(self._on_event, {"event": "closed"})

    def _on_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "ready":
            self._send({"cmd": "user"})
            self._loop.create_task(self._watch_user(self.proc))
        elif event == "user":
            self.user = msg.get("login")
        elif event in ("reserve", "draft", "error") and msg.get("id") is not None:
            future = self._waiting.pop(str(msg["id"]), None)
            if future and not future.done():
                future.set_result(msg if event != "error"
                                  else {"ok": False, "reason": msg.get("message", "erreur de la fenêtre")})
        elif event == "closed":
            self.user = None
            for future in self._waiting.values():
                if not future.done():
                    future.set_result({"ok": False, "reason": "fenêtre Vinted fermée"})
            self._waiting.clear()
        self.on_change()

    async def _watch_user(self, proc) -> None:
        """Redemande régulièrement le compte connecté (tu peux te connecter à tout moment)."""
        while self.proc is proc and self.is_open:
            await asyncio.sleep(20)
            if not self._lock.locked():
                self._send({"cmd": "user"})

    async def reserve(self, item_id: int, url: str, auto: bool = False) -> dict:
        """Réserve l'annonce dans la fenêtre. Renvoie {"ok": bool, "reason"?, "seconds"?}."""
        now = time.monotonic()
        if auto:
            self.auto_times = [t for t in self.auto_times if now - t < 3600]
            if not self.is_open:
                return {"ok": False, "reason": "fenêtre Vinted fermée : ouvre-la pour réserver tout seul"}
            if now < self.busy_until or self._lock.locked():
                return {"ok": False, "reason": "une autre réservation est en cours"}
            if len(self.auto_times) >= AUTO_MAX_PER_HOUR:
                return {"ok": False, "reason": f"limite de {AUTO_MAX_PER_HOUR} réservations automatiques par heure"}
            self.auto_times.append(now)
        if not self.is_open:
            await self.open()
        async with self._lock:  # une seule annonce à la fois dans la fenêtre
            future = asyncio.get_running_loop().create_future()
            self._waiting[str(item_id)] = future
            self._send({"cmd": "reserve", "id": item_id, "url": url})
            try:
                result = await asyncio.wait_for(future, RESERVE_WAIT)
            except asyncio.TimeoutError:
                self._waiting.pop(str(item_id), None)
                result = {"ok": False, "reason": "pas de réponse de la fenêtre Vinted"}
        if result.get("ok"):
            # Laisse le temps de payer avant qu'une réservation automatique ne change de page.
            self.busy_until = time.monotonic() + 120
        return result

    async def create_draft(self, draft_id: str, meta: dict, photos_b64: list[str]) -> dict:
        """Brouillon sur ton compte Vinted, depuis la fenêtre connectée (voir window.create_draft)."""
        if not self.is_open:
            await self.open()
            return {"ok": False, "reason": "fenêtre Vinted ouverte à l'instant : connecte-toi dedans, puis réessaie"}
        if not self.user:
            return {"ok": False, "reason": "connecte-toi à ton compte dans la fenêtre Vinted, puis réessaie"}
        async with self._lock:
            key = f"d:{draft_id}"
            future = asyncio.get_running_loop().create_future()
            self._waiting[key] = future
            self._send({"cmd": "draft", "id": key, "meta": meta, "photos": photos_b64})
            try:
                return await asyncio.wait_for(future, DRAFT_WAIT)
            except asyncio.TimeoutError:
                self._waiting.pop(key, None)
                return {"ok": False, "reason": "pas de réponse de la fenêtre Vinted"}

    def close(self) -> None:
        if self.is_open:
            self._send({"cmd": "quit"})
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
