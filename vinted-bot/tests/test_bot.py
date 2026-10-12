import asyncio
import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vinted_bot import filters
from vinted_bot.client import RateLimited, VintedClient
from vinted_bot.config import Config, Search, load_config, parse_vinted_url
from vinted_bot.models import Item
from vinted_bot.monitor import IdClock, Monitor, weighted_cycle
from vinted_bot.notifier import format_text
from vinted_bot.store import Store


def raw_item(id, price="20.0", title="Nike Air Max", ts=None, new_price_format=True):
    price_field = {"amount": price, "currency_code": "EUR"} if new_price_format else price
    return {
        "id": id, "title": title, "price": price_field,
        "total_item_price": {"amount": str(float(price) + 1.5), "currency_code": "EUR"},
        "brand_title": "Nike", "size_title": "42", "status": "Très bon état",
        "url": f"https://www.vinted.fr/items/{id}-x",
        "photo": {"url": "https://img/x.jpg", "high_resolution": {"timestamp": ts or int(time.time())}},
        "user": {"login": "alice"}, "favourite_count": 3,
    }


B = 10_315_000_000  # ordre de grandeur réel des numéros d'annonce (octobre 2026)


def svc_item(id, price="20.00", title="Nike Air Max 90", brand="Nike", second="42 · Très bon état", promoted=False):
    """Annonce au format de l'API svc-catalogue (relevé réel de septembre-octobre 2026)."""
    return {
        "content_source": "catalog_promoted_items" if promoted else "catalog",
        "id": id, "title": title, "promoted": promoted, "favourite_count": 0,
        "item_box": {"first_line": brand or title, "second_line": second, "item_id": id},
        "photo": {"url": f"https://images1.vinted.net/t/{id}/f800/x.webp", "high_resolution": {"id": "x"}},
        "price": {"amount": price, "currency_code": "EUR"},
        "total_item_price": {"amount": f"{float(price) + 0.9:.2f}", "currency_code": "EUR"},
        "url": f"/items/{id}-nike-air-max-90",
        "user": {"business": False, "id": 1, "login": "bob"},
    }


# --- config ---------------------------------------------------------------

def test_parse_vinted_url():
    domain, params = parse_vinted_url(
        "https://www.vinted.de/catalog?search_text=air%20max&catalog[]=1206&catalog[]=79"
        "&brand_ids[]=53&price_to=60&order=relevance&time=123"
    )
    assert domain == "vinted.de"
    assert params == {"search_text": "air max", "attribute_ids[catalog]": "1206,79",
                      "attribute_ids[brand]": "53", "price_to": "60"}


def test_parse_url_maps_every_filter_to_attribute_ids():
    _, params = parse_vinted_url(
        "https://www.vinted.fr/catalog?size_ids[]=207&size_ids[]=208&status_ids[]=6&color_ids[]=1"
        "&material_ids[]=44&catalog_ids=5&search_id=999&disabled_personalization=true&currency=EUR"
    )
    assert params == {"attribute_ids[size]": "207,208", "attribute_ids[status]": "6",
                      "attribute_ids[color]": "1", "attribute_ids[material]": "44",
                      "attribute_ids[catalog]": "5"}


def test_parse_rejects_other_sites():
    with pytest.raises(ValueError):
        parse_vinted_url("https://www.leboncoin.fr/recherche?text=nike")


def test_load_example_config():
    cfg = load_config("config.example.toml")
    assert len(cfg.searches) == 2
    s = cfg.searches[0]
    assert s.domain == "vinted.fr" and s.params["attribute_ids[brand]"] == "53"
    assert s.api_params(20)["order"] == "newest_first"
    assert cfg.searches[1].params == {"search_text": "carhartt detroit jacket"}
    assert s.weight == 2 and cfg.searches[1].weight == 1 and cfg.searches[1].only_deals
    assert cfg.request_gap == 1.0 and cfg.use_direct
    assert cfg.max_age == 3600


# --- modèle et filtres ----------------------------------------------------

@pytest.mark.parametrize("new_format", [True, False])
def test_item_price_formats(new_format):
    item = Item.from_api(raw_item(5, "12.5", new_price_format=new_format), "vinted.fr")
    assert item.price == 12.5 and item.total_price == 14.0 and item.currency == "EUR"


def test_filters_accent_and_word_boundary():
    s = Search(name="s", exclude=["abime", "lot"], max_price=30)
    mk = lambda title, price=10.0: Item.from_api(raw_item(1, str(price), title), "vinted.fr")
    assert filters.accept(mk("Veste abîmée"), s)[0] is False
    assert filters.accept(mk("Veste abîme manche"), s)[0] is False
    assert filters.accept(mk("Lot de 3 t-shirts"), s)[0] is False
    assert filters.accept(mk("Pilot jacket"), s)[0] is True  # "lot" dans "pilot" n'exclut pas
    assert filters.accept(mk("Veste", 31), s)[0] is False


def test_evaluate_deal():
    d = filters.evaluate_deal(20, 40, 0.3)
    assert d.is_deal and d.discount == pytest.approx(0.5)
    assert not filters.evaluate_deal(35, 40, 0.3).is_deal
    assert not filters.evaluate_deal(20, None, 0.3).is_deal


# --- moniteur -------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.pages = []
        self.calls = []

    async def search(self, params, lane=None):
        self.calls.append(params)
        return self.pages.pop(0)

    async def close(self):
        pass


class Recorder:
    def __init__(self):
        self.sent = []

    async def send(self, item, deal, search):
        self.sent.append((item, deal))


def make_monitor(tmp_path, search, **cfg_kw):
    cfg = Config(searches=[search], deal_min_samples=3, **{"extra_photos": False, **cfg_kw})
    fake = FakeClient()
    rec = Recorder()
    mon = Monitor(cfg, Store(str(tmp_path / "db.sqlite3")), [rec], client_factory=lambda d: fake)
    return mon, fake, rec


def test_warmup_then_only_new_items(tmp_path):
    search = Search(name="s", exclude=["lot"])
    mon, fake, rec = make_monitor(tmp_path, search)
    old = B - 5_000_000  # ≈ 5 h plus tôt au rythme réel de 250 numéros/s

    async def scenario():
        fake.pages = [
            [svc_item(B + i, "40.00") for i in (3, 2, 1, 0)],                       # amorçage
            [svc_item(B + 5, "20.00"), svc_item(B + 4, "50.00"), svc_item(B + 3, "40.00")],
            [svc_item(B + 6, "10.00", "Lot chaussures"), svc_item(old, "5.00", promoted=True),
             svc_item(B + 5, "20.00")],
        ]
        assert await mon.poll(search) == []
        assert fake.calls[0]["per_page"] == "96" and fake.calls[0]["order"] == "newest_first"
        second = await mon.poll(search)
        assert fake.calls[1]["per_page"] == "20"
        third = await mon.poll(search)
        await asyncio.sleep(0)
        return second, third

    second, third = asyncio.run(scenario())
    assert [i.id for i in second] == [B + 4, B + 5]
    assert third == []  # mot exclu, vieille annonce sponsorisée, annonce déjà vue
    deals = {item.id: deal for item, deal in rec.sent}
    assert deals[B + 5].is_deal and deals[B + 5].median == 40.0
    assert not deals[B + 4].is_deal


def test_out_of_order_ids_are_not_missed(tmp_path):
    """Régression : sur le vrai Vinted, 43 % des nouveautés arrivent avec un numéro
    inférieur au plus grand déjà vu (indexation décalée). Elles doivent être signalées."""
    search = Search(name="s")
    mon, fake, rec = make_monitor(tmp_path, search)

    async def scenario():
        fake.pages = [
            [svc_item(B + 1000), svc_item(B + 900)],
            [svc_item(B + 1500), svc_item(B + 1000), svc_item(B + 900)],
            [svc_item(B + 1500), svc_item(B + 1200), svc_item(B + 1000), svc_item(B + 950)],
        ]
        await mon.poll(search)
        a = await mon.poll(search)
        b = await mon.poll(search)
        return a, b

    a, b = asyncio.run(scenario())
    assert [i.id for i in a] == [B + 1500]
    assert [i.id for i in b] == [B + 950, B + 1200]


def test_old_item_revealed_by_a_sale_is_ignored_with_clock(tmp_path):
    """Une vente dans le haut de la liste fait remonter une annonce jamais vue mais ancienne."""
    search = Search(name="s")
    mon, fake, rec = make_monitor(tmp_path, search)
    mon.clock.observe(time.time(), B + 10_000)

    async def scenario():
        fake.pages = [
            [svc_item(B + 9_000), svc_item(B + 8_000)],
            [svc_item(B + 9_500), svc_item(B + 8_000), svc_item(B + 9_000 - 3 * 3600 * 250)],
        ]
        await mon.poll(search)
        return await mon.poll(search)

    found = asyncio.run(scenario())
    assert [i.id for i in found] == [B + 9_500]
    assert found[0].age_estimated and 0 <= time.time() - found[0].created_ts < 5


def test_id_clock():
    clock = IdClock()
    assert clock.age(1) is None and clock.rate() == IdClock.DEFAULT_RATE
    clock.observe(1000.0, B)
    clock.observe(1120.0, B + 120 * 300)  # 300 numéros/s
    assert clock.rate() == pytest.approx(300)
    assert clock.age(B + 120 * 300 - 3000, now=1120.0) == pytest.approx(10)
    assert clock.age(B + 120 * 300, now=1130.0) == pytest.approx(10)  # extrapolation


def test_item_from_svc_catalogue():
    item = Item.from_api(svc_item(B, "12.50"), "vinted.fr")
    assert (item.brand, item.size, item.status) == ("Nike", "42", "Très bon état")
    assert item.url == f"https://www.vinted.fr/items/{B}-nike-air-max-90"
    assert item.price == 12.5 and item.total_price == 13.4 and item.currency == "EUR"
    assert item.seller == "bob" and item.created_ts is None and not item.promoted
    no_brand = Item.from_api(svc_item(B, title="Wax Warmer", brand="", second="Bon état"), "vinted.fr")
    assert (no_brand.brand, no_brand.size, no_brand.status) == ("", "", "Bon état")
    assert Item.from_api(svc_item(B, promoted=True), "vinted.fr").promoted


def test_only_deals_and_max_age(tmp_path):
    search = Search(name="s", only_deals=True)
    mon, fake, rec = make_monitor(tmp_path, search)

    async def scenario():
        fake.pages = [
            [raw_item(i, "40.0") for i in (10, 9, 8)],
            [raw_item(13, "15.0", ts=int(time.time()) - 7200), raw_item(12, "15.0"), raw_item(11, "38.0")],
        ]
        await mon.poll(search)
        return await mon.poll(search)

    assert [i.id for i in asyncio.run(scenario())] == [12]


def test_restart_does_not_rewarm(tmp_path):
    search = Search(name="s")
    mon, fake, _ = make_monitor(tmp_path, search)
    fake.pages = [[svc_item(B + 1)]]
    asyncio.run(mon.poll(search))
    mon2, fake2, rec2 = make_monitor(tmp_path, search)
    fake2.pages = [[svc_item(B + 2), svc_item(B + 1)]]

    async def go():
        found = await mon2.poll(search)
        await asyncio.sleep(0)
        return found

    assert [i.id for i in asyncio.run(go())] == [B + 2]
    assert len(rec2.sent) == 1


def test_item_matching_two_searches_alerts_once(tmp_path):
    a, b = Search(name="a"), Search(name="b")
    cfg = Config(searches=[a, b], deal_min_samples=3)
    fake, rec = FakeClient(), Recorder()
    mon = Monitor(cfg, Store(str(tmp_path / "db.sqlite3")), [rec], client_factory=lambda d: fake)

    async def scenario():
        fake.pages = [[raw_item(1)], [raw_item(1)], [raw_item(2), raw_item(1)], [raw_item(2), raw_item(1)]]
        for s in (a, b, a, b):
            await mon.poll(s)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert [item.id for item, _ in rec.sent] == [2]


# --- client HTTP (serveur local) -------------------------------------------

class FakeVinted(BaseHTTPRequestHandler):
    api_statuses = []
    seen_auth = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/catalog":
            self.send_response(200)
            self.send_header("Set-Cookie", "access_token_web=tok; Path=/")
            self.end_headers()
            self.wfile.write(b"<html></html>")
            return
        if not self.path.startswith("/svc-catalogue/items"):
            self.send_response(404)
            self.end_headers()
            return
        FakeVinted.seen_auth.append(self.headers.get("Authorization"))
        status = FakeVinted.api_statuses.pop(0) if FakeVinted.api_statuses else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if status == 200:
            self.wfile.write(json.dumps({"items": [raw_item(1)]}).encode())


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), FakeVinted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeVinted.api_statuses, FakeVinted.seen_auth = [], []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_client_refreshes_on_401_and_raises_on_429(server, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    async def scenario():
        c = VintedClient("vinted.fr", base_url=server, api_url=server + "/svc-catalogue/items", request_gap=0.01)
        FakeVinted.api_statuses = [401, 200]
        items = await c.search({"order": "newest_first"})
        FakeVinted.api_statuses = [429]
        with pytest.raises(RateLimited):
            await c.search({})
        lane = c.lanes[0]
        assert lane.gap == pytest.approx(0.015) and lane.session is None
        assert lane.next_at - time.monotonic() > 25  # pause de 30 s
        await c.close()
        return items

    items = asyncio.run(scenario())
    assert items[0]["id"] == 1
    assert FakeVinted.seen_auth and all(a == "Bearer tok" for a in FakeVinted.seen_auth)


# --- vitesse : cadencement, voies, pipelining -------------------------------

def test_acquire_respects_gap_per_lane():
    async def scenario():
        c = VintedClient("vinted.fr", request_gap=0.2)
        t0 = time.monotonic()
        for _ in range(3):
            await c.acquire()
        return time.monotonic() - t0

    assert asyncio.run(scenario()) >= 2 * 0.2 * 0.85


def test_lanes_multiply_throughput_and_skip_blocked_ones():
    async def scenario():
        c = VintedClient("vinted.fr", proxies=["http://a:1", "http://b:2"], request_gap=0.3)
        assert len(c.lanes) == 3 and c.requests_per_second == pytest.approx(10.0)
        c.lanes[0].blocked()
        t0 = time.monotonic()
        used = [await c.acquire() for _ in range(2)]
        assert time.monotonic() - t0 < 0.1  # deux voies libres : aucune attente
        assert c.lanes[0] not in used
        assert c.lanes[0].gap == pytest.approx(0.45)
        c.lanes[0].blocked()  # 2e réponse 429 du même épisode : ignorée
        assert c.lanes[0].gap == pytest.approx(0.45) and c.lanes[0].strikes == 1
        for _ in range(200):
            c.lanes[0].succeeded()
        assert c.lanes[0].gap == pytest.approx(0.3)  # retour au plancher

    asyncio.run(scenario())


def test_proxies_only_when_direct_disabled():
    c = VintedClient("vinted.fr", proxies=["http://a:1"], use_direct=False)
    assert [l.proxy for l in c.lanes] == ["http://a:1"]
    assert [l.proxy for l in VintedClient("vinted.fr", use_direct=False).lanes] == [None]


def test_weighted_cycle():
    a, b = Search(name="a", weight=3), Search(name="b")
    gen = weighted_cycle([a, b])
    seq = [next(gen).name for _ in range(8)]
    assert seq.count("a") == 6 and seq.count("b") == 2
    assert "aaaa" not in "".join(seq)  # réparti, pas en rafale


def test_scheduler_pipelines_slow_responses(tmp_path):
    """Vinted répond en 0,5 s mais le bot relance toutes les 0,1 s."""
    search = Search(name="s")
    cfg = Config(searches=[search], request_gap=0.1, max_inflight=10, stats_every=0)
    client = VintedClient("vinted.fr", request_gap=0.1)
    started = []

    async def slow_search(params, lane=None):
        started.append(time.monotonic())
        await asyncio.sleep(0.5)
        return [raw_item(len(started))]

    client.search = slow_search
    mon = Monitor(cfg, Store(str(tmp_path / "db.sqlite3")), [], client_factory=lambda d: client)

    async def scenario():
        try:
            await asyncio.wait_for(mon._drive("vinted.fr", [search]), timeout=0.75)
        except asyncio.TimeoutError:
            pass

    asyncio.run(scenario())
    assert len(started) >= 6  # sans pipelining : 2 au plus


# --- interface : cohabitation avec d'autres applications locales -------------

class OtherApp(BaseHTTPRequestHandler):
    """Une autre application locale (ex. un bot Leboncoin) qui occupe le port préféré."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html>Bot Leboncoin</html>")


def test_ui_skips_port_used_by_another_app_and_reuses_own_instance(tmp_path):
    from vinted_bot import app

    other = HTTPServer(("127.0.0.1", 0), OtherApp)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    taken = other.server_address[1]

    async def scenario():
        engine = app.Engine(tmp_path / "a")
        runner, url = await app.start_server(engine, taken)
        try:
            assert runner is not None and url != f"http://127.0.0.1:{taken}"
            assert (await asyncio.to_thread(app.whoami_at, url))["app"] == "VintedBot"
            # Deuxième lancement : il retrouve l'instance existante au lieu de l'autre application.
            second = app.Engine(tmp_path / "b")
            runner2, url2 = await app.start_server(second, taken)
            assert runner2 is None and url2 == url
            second.store.close()
        finally:
            await runner.cleanup()
            engine.store.close()

    try:
        asyncio.run(scenario())
    finally:
        other.shutdown()


# --- suivi : photos et disponibilité ----------------------------------------------------

from vinted_bot import tracking  # noqa: E402

# Extraits de vraies pages relevés le 11/10/2026 (données Next.js, guillemets échappés).
SOLD_PAGE = (
    '<html><head><meta property="og:title"></head><body>'
    '<img data-testid="item-photo-1--img" src="https://images1.vinted.net/t/01_A/f800/a.webp?s=1">'
    '<img data-testid="item-photo-2--img" src="https://images1.vinted.net/t/01_B/f800/b.webp?s=2">'
    '<script>self.__next_f.push([1,"\\"flash_messages.no_longer_available_sold.title\\":\\"Vendu\\""])</script>'
    '<script>self.__next_f.push([1,"33:[\\"$\\",\\"$Le1\\",null,{\\"plugins\\":[{\\"data\\":{\\"item_id\\":\\"10328210472\\",'
    '\\"seller_id\\":\\"3173658697\\",\\"theme\\":\\"SUCCESS\\",\\"title\\":\\"Vendu\\"},\\"exposures\\":[],'
    '\\"name\\":\\"buyer_item_status\\",\\"section\\":\\"sidebar\\",\\"type\\":\\"buyer_item_status\\"}]}]"])</script>'
    '<script>self.__next_f.push([1,"{\\"can_buy\\":false,\\"instant_buy\\":false,\\"is_reserved\\":false}"])</script>'
    '</body></html>'
)
LIVE_PAGE = (
    '<html><head><script type="application/ld+json">{"@type":"Product","offers":{"availability":"InStock"}}</script>'
    '</head><body>'
    '<img data-testid="item-photo-3--img" src="https://images1.vinted.net/t/01_C/f800/c.webp?s=3">'
    '<img data-testid="item-photo-1--img" src="https://images1.vinted.net/t/01_A/f800/a.webp?s=1">'
    '<img data-testid="item-photo-2--img" src="https://images1.vinted.net/t/01_B/f800/b.webp?s=2">'
    '<img data-testid="item-photo-4--img" src="https://images1.vinted.net/t/01_D/f800/d.webp?s=4">'
    '<script>self.__next_f.push([1,"\\"flash_messages.no_longer_available_sold.title\\":\\"Vendu\\""])</script>'
    '<script>self.__next_f.push([1,"{\\"data\\":{\\"closet_countdown_end_date\\":null,\\"is_sticky\\":true,'
    '\\"item_id\\":\\"10331217994\\"},\\"exposures\\":[],\\"name\\":\\"buy\\",\\"section\\":\\"sticky\\",\\"type\\":\\"buy\\"}"])</script>'
    '</body></html>'
)
ITEM_URL = "https://www.vinted.fr/items/10328210472-charmmy-kitty"


def test_page_status_on_real_markup():
    assert tracking.page_status(200, ITEM_URL, SOLD_PAGE) == tracking.SOLD
    assert tracking.page_status(200, ITEM_URL, LIVE_PAGE) == tracking.AVAILABLE  # « Vendu » des traductions ignoré
    assert tracking.page_status(404, ITEM_URL, "") == tracking.DELETED
    assert tracking.page_status(200, ITEM_URL, 'x data-dgst="NEXT_HTTP_ERROR_FALLBACK;404" y') == tracking.DELETED
    # Mur de connexion ou de vérification : aucune conclusion (fausses ventes en série chez d'autres)
    assert tracking.page_status(200, "https://www.vinted.fr/member/general/login", SOLD_PAGE) is None
    assert tracking.page_status(200, "https://www.vinted.fr/session-refresh?ref=x", "<html></html>") is None
    assert tracking.page_status(403, ITEM_URL, "") is None
    assert tracking.page_status(200, ITEM_URL, "<html>page inconnue</html>") is None
    reserved = '<x>{\\"can_buy\\":false,\\"is_reserved\\":true,\\"item_id\\":\\"1\\"}</x>'
    assert tracking.page_status(200, ITEM_URL, reserved) == tracking.RESERVED


def test_extract_photos_gallery_order_and_fallback():
    assert [u.split("/t/")[1][:4] for u in tracking.extract_photos(LIVE_PAGE)] == ["01_A", "01_B", "01_C"]
    assert len(tracking.extract_photos(LIVE_PAGE, 10)) == 4
    rsc = ('{\\"url\\":\\"https://images1.vinted.net/t/X1/f800/x.webp?s=9\\"}'
           ' https://images1.vinted.net/t/X1/310x430/x.webp'
           ' {\\"url\\":\\"https://images1.vinted.net/t/X2/f800/y.webp?s=8\\"}')
    assert tracking.extract_photos(rsc) == ["https://images1.vinted.net/t/X1/f800/x.webp?s=9",
                                            "https://images1.vinted.net/t/X2/f800/y.webp?s=8"]


def test_tracker_detects_disappearance_and_ignores_scroll_out():
    tr = tracking.Tracker()
    tr.add(5, "s", "u5")
    assert tr.observe_page("s", 1, [7, 6, 5, 4, 3, 2]) == []        # vue, repères 4, 3, 2
    assert tr.observe_page("s", 2, [8, 7, 6, 4, 3, 2]) == []        # absente une fois : pas encore
    gone = tr.observe_page("s", 3, [8, 7, 6, 4, 3, 2])              # deux fois : partie
    assert [t.item_id for t in gone] == [5] and gone[0].status == tracking.GONE
    back = tr.observe_page("s", 4, [9, 8, 7, 6, 5, 4])              # réapparue (réservation annulée…)
    assert [t.item_id for t in back] == [5] and back[0].status == tracking.AVAILABLE
    # Poussée hors de la fenêtre par des nouveautés : repères absents aussi, aucune conclusion
    tr2 = tracking.Tracker()
    tr2.add(5, "s", "u5")
    tr2.observe_page("s", 1, [6, 5, 4, 3])
    assert tr2.observe_page("s", 2, [12, 11, 10, 9]) == [] and tr2.observe_page("s", 3, [13, 12, 11, 10]) == []
    assert tr2.items[5].status == tracking.AVAILABLE
    # Réponse plus ancienne arrivée après une plus récente : ignorée
    assert tr.observe_page("s", 2, [1]) == []


class PageClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.pages_html = {}
        self.page_calls = []

    async def acquire(self):
        return None

    async def get_page(self, url, lane):
        self.page_calls.append(url)
        return self.pages_html.get(url, (200, url, LIVE_PAGE))


class TrackRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.photo_events, self.status_events = [], []

    async def send(self, item, deal, search):
        await super().send(item, deal, search)
        return {"message_id": item.id}

    async def photos(self, item_id, urls, handle):
        self.photo_events.append((item_id, urls, handle))

    async def status(self, t, handle):
        self.status_events.append((t.item_id, t.status, handle))


def test_alert_gets_photos_then_sale_is_detected_and_confirmed(tmp_path):
    search = Search(name="s")
    cfg = Config(searches=[search], deal_min_samples=3, page_gap=0)
    fake, rec = PageClient(), TrackRecorder()
    store = Store(str(tmp_path / "db.sqlite3"))
    mon = Monitor(cfg, store, [rec], client_factory=lambda d: fake)
    sold_url = f"https://www.vinted.fr/items/{B + 50}-nike-air-max-90"
    older = [svc_item(B + i) for i in (40, 30, 20, 10)]

    async def fetch_next_page():
        entry = mon._pop_page("vinted.fr")
        assert entry is not None
        sem = asyncio.Semaphore(0)
        await mon._fetch_page("vinted.fr", None, sem, *entry)
        await asyncio.sleep(0)

    async def scenario():
        fake.pages = [older, [svc_item(B + 50), *older]]
        await mon.poll(search)                       # amorçage
        alerted = await mon.poll(search)             # alerte sur B+50
        assert [i.id for i in alerted] == [B + 50]
        for _ in range(5):
            await asyncio.sleep(0)
        # Une seule alerte, envoyée avec ses 3 photos (page lue avant l'envoi)
        assert len(rec.sent) == 1 and len(rec.sent[0][0].photo_urls) == 3
        assert rec.photo_events == [] and rec.status_events == [] and mon._pop_page("vinted.fr") is None
        mon._next_page_at = 0
        # Vendue : elle disparaît alors que les annonces plus anciennes sont toujours là
        fake.pages_html[sold_url] = (200, sold_url, SOLD_PAGE)
        fake.pages = [older, older]
        await mon.poll(search)
        await mon.poll(search)
        await asyncio.sleep(0)
        assert rec.status_events[-1][:2] == (B + 50, tracking.GONE)
        await fetch_next_page()                      # confirmation par la page
        assert rec.status_events[-1][:2] == (B + 50, tracking.SOLD)
        assert mon._pop_page("vinted.fr") is None

    asyncio.run(scenario())
    saved = {r["item_id"]: r for r in store.load_tracked(0)}
    assert saved[B + 50]["status"] == tracking.SOLD
    assert saved[B + 50]["handles"]["TrackRecorder"] == {"message_id": B + 50}


def test_page_block_pauses_page_reads_only_for_a_while(tmp_path):
    search = Search(name="s")
    cfg = Config(searches=[search], page_gap=0)
    fake = PageClient()

    async def blocked(url, lane):
        raise RateLimited("HTTP 403")

    fake.get_page = blocked
    mon = Monitor(cfg, Store(str(tmp_path / "db.sqlite3")), [], client_factory=lambda d: fake)
    mon._enqueue_page(1, "https://www.vinted.fr/items/1", "vinted.fr", "photos")
    mon._enqueue_page(2, "https://www.vinted.fr/items/2", "vinted.fr", "photos")

    async def scenario():
        entry = mon._pop_page("vinted.fr")
        await mon._fetch_page("vinted.fr", None, asyncio.Semaphore(0), *entry)
        assert mon._pop_page("vinted.fr") is None          # en pause malgré une page en attente
        mon._page_pause_until = 0
        assert mon._pop_page("vinted.fr") is not None

    asyncio.run(scenario())


def test_photos_requests_expire(tmp_path):
    mon, fake, rec = make_monitor(tmp_path, Search(name="s"))
    mon._enqueue_page(1, "u", "vinted.fr", "photos")
    mon._page_queue[1]["since"] -= 1000
    assert mon._pop_page("vinted.fr") is None and not mon._page_queue


def test_store_migrates_old_alerts_and_updates(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, payload TEXT NOT NULL)")
    db.execute("INSERT INTO alerts(ts, payload) VALUES(1, ?)", (json.dumps({"id": 42, "title": "x"}),))
    db.commit()
    db.close()
    store = Store(path)
    assert store.update_alert(42, {"availability": "sold"})["availability"] == "sold"
    assert store.recent_alerts()[0] == {"id": 42, "title": "x", "availability": "sold"}
    assert store.update_alert(7, {"a": 1}) is None


def test_telegram_single_message_with_photos_reserve_button_and_tap(monkeypatch):
    import io as _io
    from PIL import Image
    from vinted_bot import notifier as nt
    calls = []

    def jpeg(color):
        out = _io.BytesIO()
        Image.new("RGB", (60, 80), color).save(out, "JPEG")
        return out.getvalue()

    class Resp:
        def __init__(self, result, status=200, content=b""):
            self.status_code, self.result, self.content, self.text = status, result, content, ""

        def json(self):
            return {"ok": True, "result": self.result}

    tg = nt.TelegramNotifier("T", "123")
    updates = [[{"update_id": 1}],  # appui ancien, d'avant le démarrage : ignoré
               [{"update_id": 2, "callback_query": {"id": "q1", "data": f"r:{B}", "message": {"chat": {"id": 999}}}},
                {"update_id": 3, "callback_query": {"id": "q2", "data": f"r:{B}", "message": {"chat": {"id": 123}}}}]]

    async def get(url, **kw):
        return Resp(None, content=jpeg((200, 30, 30)))

    async def post(url, json=None, data=None, multipart=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        calls.append((method, json if json is not None else data))
        if method == "getUpdates":
            if not updates:
                raise asyncio.CancelledError
            return Resp(updates.pop(0))
        return Resp({"message_id": 77 if method == "sendPhoto" else 99})

    tg.http.get, tg.http.post = get, post
    taps = []

    async def on_reserve(item_id):
        taps.append(item_id)

    async def scenario():
        sold = tracking.Tracked(B, "s", "u", time.time() - 420, status=tracking.SOLD, status_ts=time.time())
        item = Item.from_api(svc_item(B), "vinted.fr")
        item.photo_urls = ["p1", "p2", "p3"]
        photo = await tg.send(item, filters.evaluate_deal(20, None, 0.3), Search(name="s"))
        await tg.status(sold, photo)
        item.photo_urls = ["p1"]
        single = await tg.send(item, filters.evaluate_deal(20, None, 0.3), Search(name="s"))
        await tg.status(sold, single)
        await tg.reserved(B, {"ok": True, "seconds": 1.4, "checkout_url": "https://www.vinted.fr/checkout?purchase_id=5"},
                          photo, auto=True)
        with contextlib.suppress(asyncio.CancelledError):
            await tg.listen(on_reserve)
        await asyncio.sleep(0)
        return photo, single

    photo, single = asyncio.run(scenario())
    methods = [c[0] for c in calls]
    assert methods[:5] == ["sendPhoto", "editMessageCaption", "sendMessage", "editMessageText", "sendMessage"]
    sent = calls[0][1]
    assert json.loads(sent["reply_markup"])["inline_keyboard"][0][0] == {"text": "⚡ Réserver", "callback_data": f"r:{B}"}
    assert photo["message_id"] == 77 and photo["caption"]
    sold_edit = calls[1][1]
    assert sold_edit["caption"].startswith("<b>❌ VENDUE 7 min après l&#x27;alerte</b>")
    assert [b["text"] for b in sold_edit["reply_markup"]["inline_keyboard"][0]] == ["Voir sur Vinted"]
    assert calls[2][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "⚡ Réserver"
    paid = calls[4][1]
    assert "Réservée pour toi automatiquement" in paid["text"] and paid["reply_parameters"]["message_id"] == 77
    assert paid["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("purchase_id=5")
    assert taps == [B]  # seul l'appui venant de ton propre compte Telegram compte
    assert ("answerCallbackQuery", {"callback_query_id": "q2", "text": "⏳ Réservation sur ton ordinateur…"}) in calls


def test_collage_layout():
    import io as _io
    from PIL import Image
    from vinted_bot.notifier import collage
    imgs = [Image.new("RGB", (300, 400), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    out = Image.open(_io.BytesIO(collage(imgs)))
    assert out.size == (1200, 800)
    px = lambda x, y: out.getpixel((x, y))  # noqa: E731
    assert px(300, 400)[0] > 200 and px(1000, 200)[1] > 200 and px(1000, 600)[2] > 200


def test_likes_are_followed_live(tmp_path):
    search = Search(name="s")
    mon, fake, rec = make_monitor(tmp_path, search)

    class Likes:
        def __init__(self):
            self.seen = []

        async def send(self, item, deal, search):
            pass

        async def likes(self, t):
            self.seen.append((t.item_id, t.likes, t.views))

    likes = Likes()
    mon.notifiers.append(likes)
    older = [svc_item(B + i) for i in (40, 30, 20, 10)]
    fresh = svc_item(B + 50)

    async def scenario():
        fake.pages = [older, [fresh, *older], [dict(fresh, favourite_count=2, view_count=9), *older],
                      [dict(fresh, favourite_count=2, view_count=9), *older]]
        for _ in range(4):
            await mon.poll(search)
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert likes.seen == [(B + 50, 2, 9)]  # signalé une fois, au changement seulement


def test_still_on_sale_stops_flip_flop_and_promoted_is_not_missing(tmp_path):
    tr = tracking.Tracker()
    tr.add(5, "s", "u5")
    tr.observe_page("s", 1, [7, 6, 5, 4, 3, 2])
    # Sponsorisée : absente de la liste normale, mais pas partie
    assert tr.observe_page("s", 2, [7, 6, 4, 3, 2], {5}) == []
    assert tr.observe_page("s", 3, [7, 6, 4, 3, 2], {5}) == [] and tr.items[5].missing == 0

    # Page « encore en vente » après une disparition (prix changé…) : plus de va-et-vient
    search = Search(name="s")
    fake = PageClient()
    mon = Monitor(Config(searches=[search], page_gap=0), Store(str(tmp_path / "b.sqlite3")), [],
                  client_factory=lambda d: fake)
    t = mon.tracker.add(5, "s", "https://www.vinted.fr/items/5")
    mon.tracker.observe_page("s", 1, [7, 6, 5, 4, 3, 2])
    mon.tracker.observe_page("s", 2, [7, 6, 4, 3, 2])
    mon.tracker.observe_page("s", 3, [7, 6, 4, 3, 2])
    assert t.status == tracking.GONE
    mon._enqueue_page(5, t.url, "vinted.fr", "status")
    asyncio.run(mon._fetch_page("vinted.fr", None, asyncio.Semaphore(0), *mon._pop_page("vinted.fr")))
    assert t.status == tracking.AVAILABLE
    assert mon.tracker.observe_page("s", 4, [8, 7, 6, 4, 3]) == []
    assert mon.tracker.observe_page("s", 5, [8, 7, 6, 4, 3]) == [] and t.status == tracking.AVAILABLE


def test_old_sale_check_goes_before_new_photos(tmp_path):
    mon, fake, rec = make_monitor(tmp_path, Search(name="s"))
    mon._enqueue_page(1, "u1", "vinted.fr", "status")
    mon._page_queue[1]["since"] -= 200
    mon._enqueue_page(2, "u2", "vinted.fr", "photos")
    assert mon._pop_page("vinted.fr")[0] == 1


FAKE_WINDOW = r'''
import json, sys
print(json.dumps({"event": "ready"}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd["cmd"] == "user":
        print(json.dumps({"event": "user", "login": "alice"}), flush=True)
    elif cmd["cmd"] == "reserve":
        if cmd["id"] == 1:
            print("journal de la bibliothèque de fenêtres", flush=True)
            print(json.dumps({"event": "reserve", "id": 1, "ok": True, "seconds": 1.2,
                              "checkout_url": "https://www.vinted.fr/checkout?purchase_id=9"}), flush=True)
        else:
            print(json.dumps({"event": "reserve", "id": cmd["id"], "ok": False,
                              "reason": "quelqu'un d'autre est déjà en train de l'acheter"}), flush=True)
    elif cmd["cmd"] == "quit":
        break
'''


def test_vinted_window_reserves_and_limits_auto(tmp_path):
    import sys as _sys
    from vinted_bot import buyer
    script = tmp_path / "fake_window.py"
    script.write_text(FAKE_WINDOW, "utf-8")
    changes = []
    win = buyer.VintedWindow(tmp_path, on_change=lambda: changes.append(1), command=[_sys.executable, str(script)])

    async def scenario():
        assert (await win.reserve(5, "u", auto=True))["reason"].startswith("fenêtre Vinted fermée")
        ok = await win.reserve(1, "https://www.vinted.fr/items/1")  # ouvre la fenêtre au besoin
        assert ok["ok"] and ok["seconds"] == 1.2
        await asyncio.sleep(0.2)
        assert win.state() == {"open": True, "user": "alice"}
        # Juste après une réservation réussie, pas de réservation automatique (le temps de payer)
        assert (await win.reserve(2, "u", auto=True))["reason"] == "une autre réservation est en cours"
        win.busy_until = 0
        taken = await win.reserve(2, "u", auto=True)
        assert not taken["ok"] and "quelqu'un d'autre" in taken["reason"]
        win.auto_times = [time.monotonic()] * buyer.AUTO_MAX_PER_HOUR
        assert "limite" in (await win.reserve(3, "u", auto=True))["reason"]
        win.close()
        await asyncio.sleep(0.2)
        assert win.state() == {"open": False, "user": None}

    asyncio.run(scenario())
    assert changes


def test_auto_reserve_triggers_under_price_before_photos(tmp_path):
    search = Search(name="s", auto_reserve_max=20.0)
    mon, fake, rec = make_monitor(tmp_path, search)
    asked = []
    mon.on_auto_reserve = lambda item, s: asked.append(item.id)
    older = [svc_item(B + i) for i in (40, 30, 20, 10)]

    async def scenario():
        fake.pages = [older, [svc_item(B + 60, price="35.00"), svc_item(B + 50, price="19.00"), *older]]
        await mon.poll(search)
        await mon.poll(search)

    asyncio.run(scenario())
    assert asked == [B + 50]


def test_margin_and_min_margin_filter():
    search = Search(name="h", resale_price=130.0, min_margin=20.0)
    cheap = Item.from_api(svc_item(B, price="60.00"), "vinted.fr")   # 60,90 € avec protection
    dear = Item.from_api(svc_item(B + 1, price="90.00"), "vinted.fr")
    assert filters.margin(cheap, search) == round(130 * 0.8 - 60.9 - 6, 2)   # 37,10 €
    assert filters.accept(cheap, search)[0]
    ok, why = filters.accept(dear, search)                                   # 104 − 90,90 − 6 = 7,10 €
    assert not ok and "marge" in why
    assert filters.margin(cheap, Search(name="x")) is None
    assert "💰 Marge estimée +37 € si revendue 130 €" in format_text(cheap, filters.evaluate_deal(60, None, 0.3), search)


def test_analysis_finds_searches_where_items_sell_fast():
    from vinted_bot.analysis import analyze
    now = 1_000_000.0

    def alert(key, i, price, sold_after=None):
        return {"search_key": key, "detected_ts": now - 2 * 86400 + i * 600, "price": price, "margin": 130 * 0.8 - price - 6,
                "availability": "sold" if sold_after else "available", "availability_after_s": sold_after}

    alerts = [alert("fast", i, 50 + i, 900 if i % 2 else None) for i in range(10)]
    alerts += [alert("slow", i, 80, 3 * 86400 if i == 0 else None) for i in range(10)]
    alerts += [alert("new", 0, 40)]
    rows = analyze(alerts, [{"id": "slow", "name": "Lente"}, {"id": "fast", "name": "Rapide"},
                            {"id": "new", "name": "Nouvelle"}, {"id": "none", "name": "Vide"}], now)
    assert [r["id"] for r in rows][:2] == ["fast", "slow"]
    fast = rows[0]
    assert fast["quick_rate"] == 0.5 and fast["sold"] == 5 and fast["median_sale_s"] == 900
    assert fast["quick_price"] == 55 and fast["verdict"].startswith("🔥")
    assert rows[1]["quick_rate"] == 0 and rows[1]["verdict"].startswith("🐢")
    assert next(r for r in rows if r["id"] == "new")["verdict"].startswith("Trop tôt")
    assert next(r for r in rows if r["id"] == "none")["alerts"] == 0


def test_market_watch_follows_listings_and_finds_pepites(tmp_path):
    from vinted_bot import market
    store = Store(str(tmp_path / "m.sqlite3"))
    mw = market.MarketWatch(store, None, [{"id": "h", "name": "Herno", "query": "herno"}])
    t0 = 1_000_000.0

    def page(*specs):  # (id, prix, likes)
        return [dict(svc_item(B + i, price=f"{p:.2f}"), favourite_count=l, view_count=l * 10) for i, p, l in specs]

    old = [(i, 100.0, 0) for i in range(10, 0, -1)]
    mw.process("h", page(*old), t0)                                   # premier passage : âge inconnu
    news = [(100 + k, 100.0 + k, 0) for k in range(12)]                # 12 nouvelles annonces
    mw.process("h", page(*reversed(news), *old), t0 + 180)
    news[0] = (100, 40.0, 0)                                           # deux bradées…
    news[1] = (101, 45.0, 3)                                           # …dont une likée 3 fois
    mw.process("h", page(*reversed(news), *old), t0 + 1200)
    remaining = news[10:]                                               # 100 à 109 partent d'un coup
    mw.process("h", page(*reversed(remaining), *old), t0 + 1380)
    mw.process("h", page(*reversed(remaining), *old), t0 + 1560)       # 2e absence : confirmé
    rows = {r["item_id"]: r for r in store.market_rows(0)}
    assert all(rows[B + i]["state"] == "gone" for i in range(100, 110))
    assert rows[B + 100]["price"] == 40.0 and rows[B + 100]["fresh"] == 1
    assert rows[B + 110]["state"] == "live" and rows[B + 1]["fresh"] == 0
    assert rows[B + 101]["likes"] == 3 and rows[B + 101]["likes0"] == 0
    assert abs(rows[B + 100]["gone_ts"] - (t0 + 1200 + t0 + 1380) / 2) < 1

    rep = market.report(store.market_rows(0), [{"id": "h", "name": "Herno"}], now=t0 + 8000)
    seg = rep["segments"][0]
    assert seg["sold"] == 10 and seg["median_sold"] == 104.5           # prix de vente habituel
    # Les deux bradées sont parties en ~18 min à 40 et 45 € : 2 pépites, gain = 104,5 − coût d'achat
    gain = (104.5 - market.buy_cost(40)) + (104.5 - market.buy_cost(45))
    assert seg["pepites_per_day"] == 2 and seg["money_per_day"] == round(gain, 2)
    assert rep["fast_dear"][0]["price"] == 109.0                       # ce qui part vite et cher
    assert rep["pepites"] == []                                         # plus rien de bradé en ligne

    # Nouvelle annonce bradée : signalée tout de suite, et listée parmi les pépites en ligne
    seen = []
    mw.on_pepite = lambda item, seg, median, gain: seen.append((item.id, seg, median, round(gain, 2)))
    mw.process("h", page((120, 30.0, 0), (121, 100.0, 0), *reversed(remaining), *old), t0 + 8000)
    assert seen == [(B + 120, "h", 104.5, round(104.5 - market.buy_cost(30), 2))]
    rep = market.report(store.market_rows(0), [{"id": "h", "name": "Herno"}], now=t0 + 8100)
    assert [c["item_id"] for c in rep["pepites"]] == [B + 120]


def test_market_window_exit_is_not_a_sale(tmp_path):
    from vinted_bot import market
    store = Store(str(tmp_path / "m.sqlite3"))
    mw = market.MarketWatch(store, None, [])
    mw.process("s", [svc_item(B + i) for i in (5, 4, 3)], 0)
    mw.process("s", [svc_item(B + i) for i in (9, 8, 7)], 100)       # trop de nouveautés : 5,4,3 sortis
    mw.process("s", [svc_item(B + i) for i in (9, 8, 7)], 200)
    states = {r["item_id"]: r["state"] for r in store.market_rows(0)}
    assert states[B + 5] == states[B + 4] == states[B + 3] == "out"
    # Une annonce qui réapparaît après avoir été crue partie redevient en ligne
    mw.process("s", [svc_item(B + i) for i in (9, 7)], 300)
    mw.process("s", [svc_item(B + i) for i in (9, 7)], 400)
    assert {r["item_id"]: r["state"] for r in store.market_rows(0)}[B + 8] == "gone"
    mw.process("s", [svc_item(B + i) for i in (9, 8, 7)], 500)
    assert {r["item_id"]: r["state"] for r in store.market_rows(0)}[B + 8] == "live"


def test_radar_reads_several_pages_and_reports_what_sells(tmp_path):
    from vinted_bot import market
    store = Store(str(tmp_path / "r.sqlite3"))
    calls = []

    class Cat:
        def __init__(self):
            self.listing = []

        async def search(self, params):
            calls.append(params)
            page, per = int(params["page"]), int(params["per_page"])
            return self.listing[(page - 1) * per: page * per]

    cat = Cat()
    seg = {"id": "radar", "name": "Radar", "query": "", "price_from": 200, "radar": True}
    mw = market.MarketWatch(store, lambda d: cat, [seg])
    titles = ["Air Jordan 1 High Chicago", "iPhone 14 Pro 256 Go", "Sac Louis Vuitton Speedy 30", "Doudoune Moncler Maya"]
    brands = ["Jordan", "Apple", "Louis Vuitton", "Moncler"]

    def listing(ids):
        return [dict(svc_item(B + i, price=f"{200 + (i % 4) * 100}.00", title=titles[i % 4], brand=brands[i % 4]))
                for i in ids]

    async def scenario():
        cat.listing = listing(range(300, 0, -1))              # 300 annonces ≥ 200 € : 4 pages de 96
        await mw.poll(seg)
        assert {c["page"] for c in calls} == {"1", "2", "3", "4"} and calls[0]["price_from"] == "200"
        assert "search_text" not in calls[0]
        new = list(range(320, 300, -1))                        # 20 nouvelles, puis 12 vendues
        cat.listing = listing([*new, *range(300, 0, -1)])
        await mw.poll(seg)
        sold = set(range(310, 322, 1))
        cat.listing = listing([i for i in [*new, *range(300, 0, -1)] if i not in sold])
        await mw.poll(seg)
        await mw.poll(seg)

    asyncio.run(scenario())
    assert mw.rate["radar"] > 0                               # nouvelles annonces par heure mesurées
    rep = market.radar_report(store.market_rows(0), 200)
    assert rep["sales"] == 11                                 # 310 à 320 (321 n'a jamais existé)
    assert rep["brands"][0]["sales"] >= 2 and {b["name"] for b in rep["brands"]} <= set(brands)
    assert {c["name"] for c in rep["categories"]} <= {"Baskets", "Téléphones", "Sacs", "Doudounes & manteaux"}
    models = {m["model"] for m in rep["models"]}
    assert any("jordan 1" in m for m in models)               # 3 Jordan vendues : modèle qui ressort
    assert not any("iphone" in m for m in models)             # 2 iPhone seulement : pas assez
    assert rep["recent"][0]["price"] >= 200


def test_guess_category():
    from vinted_bot.market import guess_category
    assert guess_category("iPhone 13 Pro Max 256Go") == "Téléphones"
    assert guess_category("Sac à main Longchamp") == "Sacs"
    assert guess_category("Doudoune The North Face Nuptse") == "Doudounes & manteaux"
    assert guess_category("Nike Dunk Low Panda") == "Baskets"
    assert guess_category("Chaise design") == "Autre"
