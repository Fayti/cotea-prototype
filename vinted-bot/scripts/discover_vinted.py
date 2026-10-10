"""Exploration ponctuelle de la nouvelle API catalogue de Vinted (CI uniquement)."""
import asyncio
import json
import re

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"
API = "https://api.vinted.fr/svc-catalogue/items"


def brief(data):
    items = data.get("items") or []
    out = []
    for it in items[:6]:
        price = it.get("price")
        box = it.get("item_box") or {}
        out.append((it.get("id"), price.get("amount") if isinstance(price, dict) else price,
                    box.get("first_line"), box.get("second_line")))
    return len(items), out


async def main():
    async with AsyncSession(impersonate="chrome", timeout=20, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        for path in ("/", "/catalog"):
            r = await s.get(WWW + path)
            print("GET", path, r.status_code, "cookies:", sorted(s.cookies.keys()))
        token = s.cookies.get("access_token_web")
        print("TOKEN?", bool(token), (token or "")[:12])
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        base = {"search_text": "nike", "order": "newest_first", "per_page": "5", "page": "1"}

        r = await s.get(API, params=base, headers={"Accept": "application/json"})
        print("NO-AUTH", r.status_code, r.text[:200])
        r = await s.get(API, params=base, headers=auth)
        print("AUTH", r.status_code)
        data = r.json() if r.status_code == 200 else {}
        print("TOP KEYS", list(data.keys()))
        if data.get("items"):
            print("FIRST ITEM", json.dumps(data["items"][0], ensure_ascii=False)[:3500])
        for k, v in data.items():
            if k != "items":
                print("META", k, json.dumps(v, ensure_ascii=False)[:600])
        print("ORDER ids", [i.get("id") for i in data.get("items", [])])

        variants = {
            "price_to=10": {"price_to": "10"},
            "price_to=10&currency": {"price_to": "10", "currency": "EUR"},
            "brand_ids=53": {"brand_ids": "53"},
            "brand_ids[]=53": {"brand_ids[]": "53"},
            "attribute_ids[brand]=53": {"attribute_ids[brand]": "53"},
            "catalog_ids=1206": {"catalog_ids": "1206"},
            "catalog[]=1206": {"catalog[]": "1206"},
            "attribute_ids[catalog]=1206": {"attribute_ids[catalog]": "1206"},
            "status_ids=6": {"status_ids": "6"},
            "attribute_ids[status]=6": {"attribute_ids[status]": "6"},
            "size_ids=207": {"size_ids": "207"},
            "attribute_ids[size]=207": {"attribute_ids[size]": "207"},
            "per_page=96": {"per_page": "96"},
        }
        for label, extra in variants.items():
            p = {**base, **extra}
            r = await s.get(API, params=p, headers=auth)
            if r.status_code == 200:
                print("VAR", label, 200, brief(r.json()))
            else:
                print("VAR", label, r.status_code, r.text[:200])
            await asyncio.sleep(0.5)

        # Que demande le site lui-même ? Cherche svc-catalogue dans ses scripts.
        r = await s.get(WWW + "/catalog?search_text=nike&brand_ids[]=53&order=newest_first")
        html = r.text
        print("CATALOG PAGE", r.status_code, r.url)
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
        hits = set()
        for src in scripts[:80]:
            url = src if src.startswith("http") else WWW + src
            try:
                js = (await s.get(url)).text
            except Exception:
                continue
            for m in re.finditer(r"svc-catalogue", js):
                hits.add(js[max(0, m.start() - 200): m.end() + 300].replace("\n", " "))
            for m in re.finditer(r"attribute_ids", js):
                hits.add(js[max(0, m.start() - 150): m.end() + 150].replace("\n", " "))
        for h in sorted(hits)[:25]:
            print("JS>", h)
        print("HTML svc-catalogue count", html.count("svc-catalogue"), "attribute_ids", html.count("attribute_ids"))


asyncio.run(main())
