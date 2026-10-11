"""Sonde temporaire (CI) : lien d'achat et compteur de favoris sur une vraie annonce."""
import asyncio
import json
import re

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"


def around(flat, pattern, width=200, n=3):
    return [flat[max(0, m.start() - width):m.end() + width].replace("\n", " ")
            for m in list(re.finditer(pattern, flat))[:n]]


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get(WWW + "/catalog")
        await asyncio.sleep(3)
        r = await s.get("https://api.vinted.fr/svc-catalogue/items", params={"order": "newest_first", "per_page": "20"},
                        headers={"Authorization": f"Bearer {s.cookies.get('access_token_web')}", "Accept": "application/json"})
        items = [i for i in r.json()["items"] if not i.get("promoted")]
        print("KEYS", sorted(items[0].keys()))
        print("ITEM", json.dumps(items[0], ensure_ascii=False)[:1500])
        print("FAVS", [(i["id"], i.get("favourite_count"), i.get("view_count")) for i in items])
        await asyncio.sleep(6)
        iid = items[0]["id"]
        r = await s.get(f"{WWW}/items/{iid}")
        flat = r.text.replace('\\"', '"').replace('\\\\', '\\').replace("\\u0026", "&")
        print("PAGE", r.status_code, len(flat))
        for pat in (r"transaction/buy[^\"' ]*", r"/checkout[^\"' ]*", r'"name":"buy"', r"favourite_count", r'"favourites?_?count"',
                    r'data-testid="item-buy-button"', r'buy-button', r"source_screen", r"purchase"):
            found = re.findall(pat, flat)
            print(f"\n[{pat}] x{len(found)} uniques={sorted(set(found))[:5]}")
            for c in around(flat, pat, n=2):
                print("   |", c[:450])


asyncio.run(main())
