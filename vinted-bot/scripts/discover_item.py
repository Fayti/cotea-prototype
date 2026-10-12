"""Sonde temporaire (CI) : radar + vérification des départs, comme dans l'application, 14 minutes."""
import asyncio
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vinted_bot import market  # noqa: E402
from vinted_bot.config import Config  # noqa: E402
from vinted_bot.monitor import Monitor  # noqa: E402
from vinted_bot.store import Store  # noqa: E402


async def main():
    store = Store("/tmp/radar.sqlite3")
    mon = Monitor(Config(searches=[], page_gap=10), store, [])
    mon.page_domains = {"vinted.fr"}
    mon.on_confirm = store.market_confirm
    mw = market.MarketWatch(store, mon.client, market.radar_bands(200), poll_every=150,
                            on_gone=lambda iid, url, d: mon._enqueue_page(iid, url, d, "confirm"))
    tasks = [asyncio.create_task(mon.run()), asyncio.create_task(mw.run())]
    await asyncio.sleep(840)
    for t in tasks:
        t.cancel()
    await asyncio.sleep(1)
    rows = store.market_rows(0)
    print("états :", collections.Counter(r["state"] for r in rows))
    print("blocages :", len(mon.stats.block_times), "file de vérification restante :", len(mon._page_queue))
    print("par tranche :", [(b, round(mw.rate.get(b, 0)), mw.pages.get(b), mw.window_hours(b), mw.errors.get(b))
                            for b in ("radar-1", "radar-2", "radar-3")])
    rep = market.radar_report(rows, 200)
    print("ventes confirmées :", rep["sales"], "en attente :", rep["pending"], "supprimées :", rep["deleted"],
          "encore en vente :", rep["hidden"])
    print("marques :", [(b["name"], b["sales"]) for b in rep["brands"][:10]])
    print("catégories :", [(c["name"], c["sales"]) for c in rep["categories"][:10]])
    print("ventes :", [(r["title"][:35], r["price"], r["sell_s"]) for r in rep["recent"][:12]])


asyncio.run(main())
