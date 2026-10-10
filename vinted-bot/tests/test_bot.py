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
from vinted_bot.monitor import Monitor, weighted_cycle
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


# --- config ---------------------------------------------------------------

def test_parse_vinted_url():
    domain, params = parse_vinted_url(
        "https://www.vinted.de/catalog?search_text=air%20max&catalog[]=1206&catalog[]=79"
        "&brand_ids[]=53&price_to=60&order=relevance&time=123"
    )
    assert domain == "vinted.de"
    assert params == {"search_text": "air max", "catalog_ids": "1206,79", "brand_ids": "53", "price_to": "60"}


def test_parse_rejects_other_sites():
    with pytest.raises(ValueError):
        parse_vinted_url("https://www.leboncoin.fr/recherche?text=nike")


def test_load_example_config():
    cfg = load_config("config.example.toml")
    assert len(cfg.searches) == 2
    s = cfg.searches[0]
    assert s.domain == "vinted.fr" and s.params["brand_ids"] == "53"
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

    async def scenario():
        fake.pages = [
            [raw_item(i, "40.0") for i in (103, 102, 101, 100)],               # amorçage
            [raw_item(i, p) for i, p in ((105, "20.0"), (104, "50.0"), (103, "40.0"))],
            [raw_item(106, "10.0", "Lot chaussures"), raw_item(50, "5.0"), raw_item(105, "20.0")],
        ]
        assert await mon.poll(search) == []
        assert fake.calls[0]["per_page"] == "96" and fake.calls[0]["order"] == "newest_first"
        second = await mon.poll(search)
        assert fake.calls[1]["per_page"] == "20"
        third = await mon.poll(search)
        await asyncio.sleep(0)
        return second, third

    second, third = asyncio.run(scenario())
    assert [i.id for i in second] == [104, 105]
    assert third == []  # 106 exclu par mot-clé, 50 = vieille annonce remontée, 105 déjà vue
    deals = {item.id: deal for item, deal in rec.sent}
    assert deals[105].is_deal and deals[105].median == 40.0
    assert not deals[104].is_deal


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
    fake.pages = [[raw_item(1)]]
    asyncio.run(mon.poll(search))
    mon2, fake2, rec2 = make_monitor(tmp_path, search)
    fake2.pages = [[raw_item(2), raw_item(1)]]

    async def go():
        found = await mon2.poll(search)
        await asyncio.sleep(0)
        return found

    assert [i.id for i in asyncio.run(go())] == [2]
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
    seen_cookies = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Set-Cookie", "access_token_web=tok; Path=/")
            self.end_headers()
            self.wfile.write(b"<html></html>")
            return
        FakeVinted.seen_cookies.append(self.headers.get("Cookie"))
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
    FakeVinted.api_statuses, FakeVinted.seen_cookies = [], []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_client_refreshes_on_401_and_raises_on_429(server, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    async def scenario():
        c = VintedClient("vinted.fr", base_url=server, request_gap=0.01)
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
    assert all("access_token_web=tok" in (c or "") for c in FakeVinted.seen_cookies)


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
