import asyncio
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
    cfg = Config(searches=[search], deal_min_samples=3, **cfg_kw)
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
