from fastapi.testclient import TestClient

from ghostloc_desktop.app import build_app
from ghostloc_desktop.controller import Controller
from ghostloc_desktop.device import DemoLink
from tests.test_controller import FakeAccount


def client(host="127.0.0.1"):
    ctl = Controller(FakeAccount(), DemoLink())
    app = build_app(ctl, {"siteUrl": "https://example.test"}, 8765, "secret-key")
    return TestClient(app, client=(host, 50000))


def test_lan_requires_key_and_sets_cookie():
    with client("192.168.1.20") as c:
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
