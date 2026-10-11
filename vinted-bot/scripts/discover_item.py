"""Exploration ponctuelle (CI), à rythme lent : photos d'une annonce et page d'une annonce vendue."""
import asyncio
import json
import re
import time

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"
API = "https://api.vinted.fr"
GAP = 6.0  # secondes entre deux pages d'annonce (la sonde précédente a été bloquée à ~3/s)


def page_facts(html: str) -> dict:
    facts = {"len": len(html)}
    for key in ("availability", "is_reserved", "can_buy", "is_closed", "is_hidden", "item_closing_action",
                "is_processing", "transaction_permitted", "\\\"status\\\"", "SoldOut", "OutOfStock"):
        i = html.find(key)
        facts[key] = (i, html[i:i + 70]) if i >= 0 else None
    ids = []
    for m in re.finditer(r'images1\.vinted\.net/t/([A-Za-z0-9_]+)/f800/', html):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    facts["photo_ids_f800"] = ids[:12]
    first = re.search(r'images1\.vinted\.net/t/', html)
    facts["first_photo_pos"] = first.start() if first else None
    m = re.search(r'\\"photos\\":\[', html)
    facts["photos_json_pos"] = m.start() if m else None
    return facts


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get(WWW + "/catalog")
        token = s.cookies.get("access_token_web")
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        feed = (await s.get(API + "/svc-catalogue/items", params={"order": "newest_first", "per_page": "20",
                                                                  "page": "1"}, headers=auth)).json()["items"]
        feed = [i for i in feed if not i.get("promoted")]
        a = feed[0]["id"]

        # 1. Plugins d'annonce : quelles « features » existent ?
        for feat in ("x", "summary", "attributes", "photos", "status", "transaction", "seller", "description"):
            r = await s.get(WWW + f"/api/v2/items/{a}/plugins/{feat}", headers=auth)
            print("PLUGIN", feat, r.status_code, r.text[:400].replace("\n", " "))
            await asyncio.sleep(2)

        # 2. Le paramètre ids filtre-t-il la recherche ?
        three = [i["id"] for i in feed[3:6]]
        for name in ("ids", "ids[]", "item_ids", "item_ids[]", "attribute_ids[id]", "id"):
            r = await s.get(API + "/svc-catalogue/items", params={name: ",".join(map(str, three)), "per_page": "20"},
                            headers=auth)
            got = [i["id"] for i in r.json().get("items", [])] if r.status_code == 200 else r.status_code
            print("IDS", name, "->", got if isinstance(got, int) else (len(got), set(three) <= set(got), got[:5]))
            await asyncio.sleep(2)

        # 3. Structure d'une page d'annonce : où sont les photos et l'état ?
        r = await s.get(WWW + f"/items/{a}")
        print("PAGE", r.status_code, r.url)
        print("FACTS", json.dumps(page_facts(r.text), ensure_ascii=False)[:2500])
        await asyncio.sleep(GAP)
        r = await s.head(WWW + f"/items/{a}")
        print("HEAD", r.status_code, dict(list(r.headers.items())[:6]))
        await asyncio.sleep(GAP)
        r = await s.get(WWW + f"/items/{a}", headers={"Range": "bytes=0-200000"})
        print("RANGE", r.status_code, len(r.text))
        await asyncio.sleep(GAP)

        # 4. Ventes : 20 annonces récentes à moins de 3 €, vérifiées maintenant puis dans 10 min, lentement
        cheap = (await s.get(API + "/svc-catalogue/items", params={"order": "newest_first", "per_page": "40",
                                                                   "page": "1", "price_to": "3"}, headers=auth)).json()
        watch = [i["id"] for i in cheap["items"] if not i.get("promoted")][:20]

        async def check(iid):
            pr = await s.get(WWW + f"/items/{iid}")
            f = page_facts(pr.text) if pr.status_code == 200 else {}
            return (pr.status_code, str(pr.url)[-40:], f.get("availability", (0, ""))[1][:40] if f.get("availability") else None,
                    f.get("is_reserved", (0, ""))[1][:40] if f.get("is_reserved") else None,
                    f.get("can_buy", (0, ""))[1][:30] if f.get("can_buy") else None, len(f.get("photo_ids_f800", [])))

        before = {}
        for iid in watch:
            before[iid] = await check(iid)
            await asyncio.sleep(GAP)
        print("AVANT", json.dumps(before)[:1500])
        r = await s.get(API + "/svc-catalogue/items", params={"order": "newest_first", "per_page": "5"}, headers=auth)
        print("RECHERCHE après 20 pages lentes :", r.status_code)
        await asyncio.sleep(600)
        changed = 0
        for iid in watch:
            now = await check(iid)
            if now != before[iid]:
                changed += 1
                print("CHANGÉ", iid, "\n   AVANT", before[iid], "\n   APRÈS", now)
            await asyncio.sleep(GAP)
        print("CHANGEMENTS", changed, "sur", len(watch))
        r = await s.get(API + "/svc-catalogue/items", params={"order": "newest_first", "per_page": "5"}, headers=auth)
        print("RECHERCHE à la fin :", r.status_code)


asyncio.run(main())
