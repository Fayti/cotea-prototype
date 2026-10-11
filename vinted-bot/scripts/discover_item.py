"""Exploration ponctuelle (CI) : photos disponibles et détection « vendu » sur le vrai Vinted."""
import asyncio
import collections
import json
import re
import time

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"
API = "https://api.vinted.fr"


def ld_json(html):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except Exception:
            pass
    return out


def sold_markers(text):
    keys = ["is_closed", "is_sold", "is_reserved", "is_hidden", "can_buy", "is_processing", "item_closing_action",
            "availability", "SoldOut", "InStock", "OutOfStock", "Vendu", "transaction_permitted", "is_visible"]
    found = {}
    for k in keys:
        m = re.search(re.escape(k) + r'.{0,40}', text)
        if m:
            found[k] = m.group(0)[:60]
    return found


async def main():
    async with AsyncSession(impersonate="chrome", timeout=20, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get(WWW + "/catalog")
        token = s.cookies.get("access_token_web")
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # 1. Photos dans la réponse de recherche
        r = await s.get(API + "/svc-catalogue/items", params={"order": "newest_first", "per_page": "96", "page": "1"},
                        headers=auth)
        items = r.json()["items"]
        counts = collections.Counter(len(i.get("photos") or []) for i in items)
        print("PHOTOS PAR ANNONCE (recherche) :", sorted(counts.items()))
        multi = [i for i in items if len(i.get("photos") or []) > 1]
        sample = multi[0] if multi else items[0]
        print("EXEMPLE photos urls :", [p.get("url", "")[-60:] for p in sample.get("photos", [])])
        print("CLÉS photo :", sorted((sample.get("photos") or [{}])[0].keys()))

        # 2. Endpoints de détail candidats
        iid = items[5]["id"]
        cands = [
            API + f"/svc-catalogue/items/{iid}", API + f"/svc-items/items/{iid}", API + f"/svc-item/items/{iid}",
            API + f"/svc-items/{iid}", WWW + f"/api/v2/items/{iid}", WWW + f"/api/v2/items/{iid}/details",
            WWW + f"/api/v2/items/{iid}/plugins/details", API + f"/svc-catalogue/items?ids={iid}",
        ]
        for url in cands:
            rr = await s.get(url, headers=auth)
            body = rr.text
            print("TRY", rr.status_code, url.replace(str(iid), "{id}"), "|", body[:150].replace("\n", " "))
            if rr.status_code == 200 and body.startswith("{"):
                d = rr.json()
                print("   KEYS", list(d.keys())[:30])
                inner = d.get("item") or d
                if isinstance(inner, dict):
                    print("   ITEM KEYS", sorted(inner.keys())[:80])
                    print("   PHOTOS", len(inner.get("photos") or []), "| MARKERS", sold_markers(json.dumps(inner)))
            await asyncio.sleep(0.4)

        # 3. Page HTML de l'annonce : ld+json, photos, marqueurs, endpoints svc- du code du site
        page = await s.get(WWW + f"/items/{iid}")
        html = page.text
        print("PAGE", page.status_code, len(html))
        for d in ld_json(html):
            if isinstance(d, dict):
                print("LDJSON type", d.get("@type"), "| offers", json.dumps(d.get("offers"))[:300],
                      "| images", len(d.get("image") or []) if isinstance(d.get("image"), list) else d.get("image"))
        print("MARKERS page", sold_markers(html))
        svc = sorted(set(re.findall(r'["`](/svc-[a-z0-9-]+/[A-Za-z0-9_/${}.-]*)', html)))
        print("SVC dans la page", svc[:40])
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
        found = set()
        for src in scripts[:80]:
            try:
                js = (await s.get(src if src.startswith("http") else WWW + src)).text
            except Exception:
                continue
            for m in re.findall(r'["`](/svc-[a-z0-9-]+/[A-Za-z0-9_/${}.-]*)', js):
                found.add(m)
            for m in re.finditer(r'is_closed|item_closing_action|sold', js):
                pass
        print("SVC dans le JS", sorted(found)[:80])

        # 4. Observation de ventes : annonces bon marché récentes, re-vérifiées après 9 minutes
        r = await s.get(API + "/svc-catalogue/items", params={"order": "newest_first", "per_page": "96", "page": "1",
                                                               "price_to": "4"}, headers=auth)
        watch = [i["id"] for i in r.json()["items"]][:60]
        first = {}
        for wid in watch:
            pr = await s.get(WWW + f"/items/{wid}")
            first[wid] = (pr.status_code, sold_markers(pr.text), [json.dumps(d.get("offers"))[:200] for d in ld_json(pr.text) if isinstance(d, dict)])
            await asyncio.sleep(0.3)
        print("SURVEILLANCE", len(watch), "annonces à <= 4 € ; attente 9 min")
        await asyncio.sleep(540)
        changed = 0
        for wid in watch:
            pr = await s.get(WWW + f"/items/{wid}")
            now = (pr.status_code, sold_markers(pr.text), [json.dumps(d.get("offers"))[:200] for d in ld_json(pr.text) if isinstance(d, dict)])
            if now != first[wid]:
                changed += 1
                if changed <= 6:
                    print("CHANGÉ", wid, "\n   AVANT", first[wid], "\n   APRÈS", now)
                    for url in (WWW + f"/api/v2/items/{wid}", API + f"/svc-catalogue/items/{wid}"):
                        rr = await s.get(url, headers=auth)
                        print("   ", rr.status_code, url.split('/')[-3:], rr.text[:300].replace("\n", " "))
            await asyncio.sleep(0.3)
        print("CHANGEMENTS", changed, "sur", len(watch))
        sold_example = next(iter(first.values()))
        print("EXEMPLE non changé", sold_example)


asyncio.run(main())
