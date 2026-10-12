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
    mw = market.MarketWatch(store, lambda d: client, market.radar_bands(200), poll_every=150)
    task = asyncio.create_task(mw.run())
    await asyncio.sleep(720)
    task.cancel()
    rows = store.market_rows(0)
    print("états :", collections.Counter(r["state"] + ("/fresh" if r["fresh"] else "") for r in rows))
    print("par tranche (nouvelles/h, pages, fenêtre h, erreur) :", [(b, round(mw.rate.get(b, 0)), mw.pages.get(b), mw.window_hours(b), mw.errors.get(b)) for b in ("radar-1", "radar-2", "radar-3")])
    print("prix < 200 :", sum(1 for r in rows if r["price"] < 200))
    rep = market.radar_report(rows, 200)
    print("ventes :", rep["sales"], "chiffre :", rep["revenue"])
    print("marques :", [(b["name"], b["sales"], b["median_price"]) for b in rep["brands"][:12]])
    print("catégories :", [(c["name"], c["sales"], c["median_price"]) for c in rep["categories"][:12]])
    print("modèles :", [(m["model"], m["sales"]) for m in rep["models"][:12]])
    print("dernières ventes :", [(r["title"][:35], r["price"], r["sell_s"]) for r in rep["recent"][:12]])
    # Contrôle : que sont devenues vraiment les annonces comptées comme vendues ?
    from vinted_bot.tracking import page_status
    gone = [r for r in rows if r["state"] == "gone"][:12]
    verdicts = collections.Counter()
    for r in gone:
        await asyncio.sleep(8)
        try:
            code, final_url, page = await client.get_page(r["url"], await client.acquire())
            v = page_status(code, final_url, page) or f"illisible ({code})"
        except Exception as e:
            v = f"erreur {type(e).__name__}"
        verdicts[v] += 1
        print("  contrôle :", r["title"][:35], r["price"], "->", v, flush=True)
    print("vérification des « ventes » :", dict(verdicts))
    await client.close()


asyncio.run(main())
