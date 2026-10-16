"""Sonde temporaire (CI) : où trouver la description et les avis du vendeur d'une annonce."""
import asyncio
import json
import re

from curl_cffi.requests import AsyncSession

API = "https://api.vinted.fr/svc-catalogue/items"
KEYS = ("description", "feedback_count", "feedback_reputation", "positive_feedback_count",
        "negative_feedback_count", "reviews", "review_count", "rating", "user_feedback")


def snippets(page, word, n=3, span=220):
    out = []
    for m in re.finditer(re.escape(word), page):
        out.append(page[max(0, m.start() - span):m.end() + span].replace("\n", " "))
        if len(out) >= n:
            break
    return out


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        r = await s.get("https://www.vinted.fr/catalog")
        tok = s.cookies.get("access_token_web")
        print("catalog", r.status_code, "token", bool(tok))
        auth = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        r = await s.get(API, params={"search_text": "ps5", "order": "newest_first", "per_page": "96", "page": "1"},
                        headers=auth)
        items = r.json().get("items") or []
        print("api", r.status_code, len(items))
        if items:
            print("ITEM KEYS", sorted(items[0].keys()))
            print("USER", json.dumps(items[0].get("user"), ensure_ascii=False)[:800])
            print("ITEM0", json.dumps(items[0], ensure_ascii=False)[:2500])
        for it in items[:12]:
            print("-", it["id"], it.get("title"), it.get("price"), it.get("url") or it.get("path"))
        for it in items[:3]:
            await asyncio.sleep(12)
            url = it.get("url") or it.get("path")
            if url.startswith("/"):
                url = "https://www.vinted.fr" + url
            r = await s.get(url, headers={"Referer": "https://www.vinted.fr/catalog"})
            page = r.text
            print("\n=== PAGE", it["id"], r.status_code, r.url, len(page))
            flat = page.replace('\\"', '"')
            for k in KEYS:
                found = snippets(flat, f'"{k}"', n=2)
                print(f"  [{k}] {len(re.findall(re.escape(chr(34) + k + chr(34)), flat))} occurrences")
                for sn in found:
                    print("     ", sn[:460])
            for word in ("avis", "évaluation", "Evaluation", "paypal", "PayPal"):
                c = len(re.findall(word, page))
                print(f"  mot {word!r}: {c}")
                for sn in snippets(page, word, n=1, span=150):
                    print("     ", sn[:320])
            uid = (it.get("user") or {}).get("id")
            for api in (f"https://www.vinted.fr/api/v2/users/{uid}", f"https://www.vinted.fr/api/v2/items/{it['id']}/details",
                        f"https://www.vinted.fr/api/v2/items/{it['id']}", f"https://www.vinted.fr/api/v2/user_feedbacks?user_id={uid}&page=1&per_page=5"):
                await asyncio.sleep(4)
                rr = await s.get(api, headers={**auth, "Referer": url})
                body = rr.text
                print("  API", api, rr.status_code, len(body))
                try:
                    d = rr.json()
                    print("     keys", list(d.keys())[:20])
                    flatd = json.dumps(d, ensure_ascii=False)
                    for k in KEYS:
                        for m in re.finditer(f'"{k}": ?[^,}}]{{0,80}}', flatd):
                            print("     ", m.group(0)[:120])
                            break
                except Exception:
                    print("     non JSON", body[:150])


asyncio.run(main())
