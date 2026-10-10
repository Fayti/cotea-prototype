"""Exploration ponctuelle de la nouvelle API catalogue de Vinted (CI uniquement)."""
import asyncio
import collections
import json
import statistics
import time

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"
API = "https://api.vinted.fr/svc-catalogue/items"


def strip(item):
    return {k: v for k, v in item.items() if k not in ("photos",)} | {
        "photo": {k: v for k, v in (item.get("photo") or {}).items() if k not in ("thumbnails",)}}


async def main():
    async with AsyncSession(impersonate="chrome", timeout=20, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get(WWW + "/catalog")
        token = s.cookies.get("access_token_web")
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        async def get(params):
            r = await s.get(API, params=params, headers=auth)
            return r.status_code, (r.json() if r.status_code == 200 else r.text[:200])

        st, data = await get({"order": "newest_first", "per_page": "96", "page": "1"})
        items = data["items"]
        print("KEYS", sorted(items[0].keys()))
        for it in items[:2]:
            print("ITEM", json.dumps(strip(it), ensure_ascii=False)[:2500])
        print("CONTENT_SOURCE", collections.Counter(i.get("content_source") for i in items))
        odd = [i for i in items if i.get("content_source") != "search"][:3]
        for it in odd:
            print("ODD", json.dumps(strip(it), ensure_ascii=False)[:1500])
        ids = [i["id"] for i in items]
        print("IDS", ids[:30])

        for label, p in {
            "brand_ids=53": {"brand_ids": "53"},
            "attribute_ids[brand]=53": {"attribute_ids[brand]": "53"},
            "attribute_ids[brand]=53,14": {"attribute_ids[brand]": "53,14"},
            "attribute_ids[size]=207,208": {"attribute_ids[size]": "207,208"},
            "attribute_ids[color]=1": {"attribute_ids[color]": "1"},
            "attribute_ids[material]=44": {"attribute_ids[material]": "44"},
            "price_from=500": {"price_from": "500"},
        }.items():
            st, d = await get({"order": "newest_first", "per_page": "20", "page": "1", **p})
            if st == 200:
                rows = [((i.get("item_box") or {}).get("first_line"), (i.get("item_box") or {}).get("second_line"),
                         (i.get("price") or {}).get("amount")) for i in d["items"][:8]]
                print("VAR", label, collections.Counter(r[0] for r in rows).most_common(4), rows[:4])
            else:
                print("VAR", label, st, d)
            await asyncio.sleep(0.5)

        # Expérience : comment arrivent les nouvelles annonces ? (recherche large, 150 s)
        seen, max_id, events = set(ids), max(ids), []
        start_max, t0, polls, rtts = max_id, time.time(), 0, []
        while time.time() - t0 < 150:
            q0 = time.monotonic()
            st, d = await get({"order": "newest_first", "per_page": "96", "page": "1"})
            rtts.append(time.monotonic() - q0)
            polls += 1
            if st != 200:
                print("POLL", st, d)
                await asyncio.sleep(5)
                continue
            for pos, it in enumerate(d["items"]):
                if it["id"] not in seen:
                    seen.add(it["id"])
                    events.append((it["id"] - max_id, pos, it.get("content_source")))
            max_id = max(max_id, *(i["id"] for i in d["items"]))
            await asyncio.sleep(2.5)
        mins = (time.time() - t0) / 60
        deltas = [e[0] for e in events]
        print(f"POLLS {polls} rtt médian {statistics.median(rtts)*1000:.0f} ms | nouvelles {len(events)} en {mins:.1f} min"
              f" | ids/min {(max_id - start_max)/mins:.0f}")
        if deltas:
            neg = [d for d in deltas if d < 0]
            print("DELTA<0", len(neg), "sur", len(deltas), "| quantiles",
                  [round(q) for q in statistics.quantiles(deltas, n=10)] if len(deltas) > 10 else sorted(deltas))
            print("MIN DELTAS", sorted(deltas)[:15])
            print("POS des nouvelles", collections.Counter(min(e[1] // 10, 9) for e in events))
            print("SOURCES nouvelles", collections.Counter(e[2] for e in events))


asyncio.run(main())
