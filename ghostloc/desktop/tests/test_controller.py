import asyncio

import pytest

from ghostloc_desktop import controller as C
from ghostloc_desktop.account import AccountError
from ghostloc_desktop.device import DemoLink


class FakeAccount:
    """Imite le serveur : quota en secondes, refus quand il est épuisé."""

    def __init__(self, plan="essentiel", remaining=3600):
        self.email = None
        self.configured = True
        self.plan, self.remaining = plan, remaining
        self.calls = []
        self.offline = False

    def _ent(self, stop=False):
        return {"plan": self.plan, "active": True, "routes": self.plan == "illimite",
                "remaining_seconds": None if self.plan == "illimite" else self.remaining, "stop": stop}

    async def login(self, email, password):
        if password != "ok":
            raise AccountError("invalid_credentials")
        self.email = email

    def logout(self):
        self.email = None

    async def entitlement(self):
        return self._ent()

    async def session_start(self, device):
        self.calls.append("start")
        if self.remaining <= 0:
            raise AccountError("quota_exhausted")
        return {**self._ent(), "session_id": "s1"}

    async def session_heartbeat(self, sid):
        self.calls.append("heartbeat")
        if self.offline:
            raise OSError("offline")
        return self._ent(stop=self.remaining <= 0)

    async def session_stop(self, sid):
        self.calls.append("stop")
        return self._ent(stop=True)


async def ready(account):
    ctl = C.Controller(account, DemoLink())
    await ctl.connect()
    return ctl


def run(coro):
    return asyncio.run(coro)


def test_login_errors_are_translated():
    async def go():
        ctl = await ready(FakeAccount())
        with pytest.raises(C.UserError, match="mot de passe incorrect"):
            await ctl.login("a@b.c", "bad")
    run(go())


def test_teleport_requires_login_then_opens_one_session():
    async def go():
        acc = FakeAccount()
        ctl = await ready(acc)
        with pytest.raises(C.UserError, match="Connecte-toi"):
            await ctl.teleport(1, 2, "X")
        await ctl.login("a@b.c", "ok")
        await ctl.teleport(1, 2, "X")
        await ctl.teleport(3, 4, "Y")
        assert acc.calls.count("start") == 1
        assert ctl.link.positions[-1] == (3, 4)
        await ctl.go_home()
        assert ctl.location is None and acc.calls[-1] == "stop"
        assert ctl.link.positions[-1] == (0.0, 0.0)  # clear envoyé à l'iPhone
    run(go())


def test_quota_exhausted_restores_real_position(monkeypatch):
    monkeypatch.setattr(C, "HEARTBEAT_S", 0.05)

    async def go():
        acc = FakeAccount(remaining=10)
        ctl = await ready(acc)
        await ctl.login("a@b.c", "ok")
        await ctl.teleport(1, 2, "X")
        acc.remaining = 0
        await asyncio.sleep(0.3)
        assert ctl.location is None and ctl.session_id is None
        assert "épuisées" in ctl.notice
        with pytest.raises(C.UserError, match="épuisées"):
            await ctl.teleport(1, 2, "X")
    run(go())


def test_lost_internet_restores_real_position(monkeypatch):
    monkeypatch.setattr(C, "HEARTBEAT_S", 0.02)

    async def go():
        acc = FakeAccount()
        ctl = await ready(acc)
        await ctl.login("a@b.c", "ok")
        await ctl.teleport(1, 2, "X")
        acc.offline = True
        await asyncio.sleep(0.4)
        assert ctl.location is None and "Internet" in ctl.notice
    run(go())


def test_routes_reserved_to_illimite_and_move_device(monkeypatch):
    async def go():
        acc = FakeAccount()
        ctl = await ready(acc)
        await ctl.login("a@b.c", "ok")
        await ctl.teleport(0, 0, "Départ")
        with pytest.raises(C.UserError, match="Illimité"):
            await ctl.start_route(0, 0.001, "B", "avion", 1000)
        acc.plan = "illimite"
        await ctl.start_route(0, 0.01, "B", "avion", 1200)
        await asyncio.sleep(1.3)
        assert ctl.route is not None and ctl.route["done_m"] > 0
        assert ctl.link.positions[-1][1] > 0
        await ctl.stop_route()
        assert ctl.route is None and ctl.location is not None
        await ctl.go_home()
    run(go())
