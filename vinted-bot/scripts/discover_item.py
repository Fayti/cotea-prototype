"""Sonde temporaire (CI) : la veille du marché sur le vrai Vinted pendant 12 minutes."""
import asyncio
import collections
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vinted_bot import market  # noqa: E402
from vinted_bot.client import VintedClient  # noqa: E402
from vinted_bot.store import Store  # noqa: E402

SEGMENTS = [{"id": q, "name": q, "query": q} for q in ("patagonia", "lululemon", "carhartt", "levis 501", "nintendo")]


async def main():
    store = Store("/tmp/probe.sqlite3")
    client = VintedClient("vinted.fr", request_gap=1.0)
    pepites = []
    mw = market.MarketWatch(store, lambda d: client, SEGMENTS, poll_every=60,
                            on_pepite=lambda i, s, m, g: pepites.append((s, i.title, i.price, m, g)))
    task = asyncio.create_task(mw.run())
    await asyncio.sleep(720)
    task.cancel()
    rows = store.market_rows(0)
    by = collections.defaultdict(collections.Counter)
    for r in rows:
        by[r["segment"]][r["state"] + ("/fresh" if r["fresh"] else "")] += 1
    for seg, c in by.items():
        print(seg, dict(c), "erreur:", mw.errors.get(seg))
    rep = market.report(rows, SEGMENTS)
    for s in rep["segments"]:
        print({k: s[k] for k in ("name", "listings", "new_per_day", "sold", "median_sold", "median_list", "median_sell_s", "likes_per_h")})
    gone = [r for r in rows if r["state"] == "gone" and r["fresh"]]
    print("exemples partis :", [(r["title"][:30], r["price"], round(r["gone_ts"] - r["first_seen"])) for r in gone[:8]])
    liked = sorted(rows, key=lambda r: -(r["likes"] - r["likes0"]))[:5]
    print("likes gagnés :", [(r["title"][:25], r["likes0"], r["likes"]) for r in liked])
    print("pépites :", pepites[:5])
    await client.close()


asyncio.run(main())
