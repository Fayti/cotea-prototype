from fastapi.testclient import TestClient

from ghostloc_desktop.app import build_app
from ghostloc_desktop.controller import Controller
from ghostloc_desktop.device import DemoLink
from tests.test_controller import FakeAccount


def client(host="127.0.0.1", base="http://127.0.0.1:8765"):
    ctl = Controller(FakeAccount(), DemoLink())
    app = build_app(ctl, {"siteUrl": "https://example.test"}, 8765, "secret-key")
    return TestClient(app, client=(host, 50000), base_url=base, headers={"X-GhostLoc": "1"})


def test_lan_requires_key_and_sets_cookie(monkeypatch):
    monkeypatch.setattr("ghostloc_desktop.app.lan_ip", lambda: "192.168.1.10")
    with client("192.168.1.20", "http://192.168.1.10:8765") as c:
        assert c.get("/api/state").status_code == 403
        assert c.get("/?k=wrong").status_code == 403
        r = c.get("/?k=secret-key")
        assert r.status_code == 200 and "glk" in r.cookies
        assert c.get("/api/state").status_code == 200  # le cookie suffit ensuite


def test_invalid_coordinates_are_rejected():
    with client() as c:
        r = c.post("/api/teleport", json={"lat": "x", "lng": 2})
        assert r.status_code == 400 and r.json()["error"] == "Coordonnées invalides."
        assert c.post("/api/teleport", json={"lat": 120, "lng": 2}).status_code == 400


def test_state_exposes_lan_url():
    with client() as c:
        state = c.get("/api/state").json()
        assert state["app"]["lanUrl"].endswith("/?k=secret-key")


def test_cross_site_post_and_dns_rebinding_are_blocked():
    with client() as c:
        assert c.post("/api/home", headers={"X-GhostLoc": ""}).status_code == 403
        assert c.post("/api/home").status_code == 200
    with client(base="http://evil.example:8765") as c:
        assert c.get("/api/state").status_code == 403


def test_diagnostic_has_versions_but_no_secrets():
    with client() as c:
        text = c.get("/api/diagnostic").json()["text"]
        assert text.startswith("GhostLoc ") and "iPhone :" in text
        assert "secret-key" not in text
