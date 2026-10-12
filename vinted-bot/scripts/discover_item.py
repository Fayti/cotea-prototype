"""Sonde temporaire (CI) : le radar ≥ 200 sur le vrai Vinted pendant 15 minutes."""
import asyncio
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vinted_bot import market  # noqa: E402
from vinted_bot.client import VintedClient  # noqa: E402
from vinted_bot.store import Store  # noqa: E402

SEG = {"id": "radar", "name": "Radar", "query": "", "price_from": 200, "radar": True}


async def main():
    store = Store("/tmp/radar.sqlite3")
    client = VintedClient("vinted.fr", request_gap=1.0)
    # Contrôle direct de la pagination et du filtre de prix
    pages = []
    for p in (1, 2, 5, 20):
        r = await client.search({"price_from": "200", "order": "newest_first", "per_page": "96", "page": str(p)})
        prices = [float(i["price"]["amount"]) for i in r]
        pages.append((p, len(r), min(prices) if prices else None, r[0]["id"] if r else None, r[-1]["id"] if r else None))
    print("pages (n, prix min, premier id, dernier id) :", pages)
    mw = market.MarketWatch(store, lambda d: client, [SEG], poll_every=150)
    task = asyncio.create_task(mw.run())
    await asyncio.sleep(900)
    task.cancel()
    rows = store.market_rows(0)
    print("états :", collections.Counter(r["state"] + ("/fresh" if r["fresh"] else "") for r in rows))
    print("nouvelles/h :", round(mw.rate.get("radar", 0)), "pages lues :", mw.pages.get("radar"), "erreur :", mw.errors.get("radar"))
    print("prix < 200 :", sum(1 for r in rows if r["price"] < 200))
    rep = market.radar_report(rows, 200)
    print("ventes :", rep["sales"], "chiffre :", rep["revenue"])
    print("marques :", [(b["name"], b["sales"], b["median_price"]) for b in rep["brands"][:12]])
    print("catégories :", [(c["name"], c["sales"], c["median_price"]) for c in rep["categories"][:12]])
    print("modèles :", [(m["model"], m["sales"]) for m in rep["models"][:12]])
    print("dernières ventes :", [(r["title"][:35], r["price"], r["sell_s"]) for r in rep["recent"][:12]])
    await client.close()


asyncio.run(main())
