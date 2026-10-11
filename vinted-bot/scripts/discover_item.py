"""Vérification (CI) du code d'analyse des pages sur de vraies annonces vendues et disponibles."""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from curl_cffi.requests import AsyncSession  # noqa: E402

from vinted_bot.tracking import extract_photos, page_status  # noqa: E402

WWW = "https://www.vinted.fr"
# Vendues entre deux relevés de la sonde du 11/10 (can_buy passé à false), puis une disponible.
CASES = {10328214268: "vendue ?", 10328211304: "vendue ?", 10328210472: "vendue ?", 10328213539: "disponible ?"}


def around(flat, pattern, width=260):
    out = []
    for m in re.finditer(pattern, flat):
        out.append(flat[max(0, m.start() - width):m.end() + width].replace("\n", " "))
        if len(out) >= 2:
            break
    return out


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get(WWW + "/catalog")
        await asyncio.sleep(3)
        # une annonce toute fraîche, sûrement disponible
        r = await s.get("https://api.vinted.fr/svc-catalogue/items", params={"order": "newest_first", "per_page": "5"},
                        headers={"Authorization": f"Bearer {s.cookies.get('access_token_web')}", "Accept": "application/json"})
        fresh = [i["id"] for i in r.json()["items"] if not i.get("promoted")][0]
        CASES[fresh] = "toute fraîche"
        for iid, label in CASES.items():
            await asyncio.sleep(6)
            r = await s.get(f"{WWW}/items/{iid}")
            page = r.text
            flat = page.replace('\\"', '"').replace('\\\\', '\\')
            print(f"\n=== {iid} ({label}) HTTP {r.status_code} {str(r.url)[-50:]} len={len(page)}")
            print("  page_status ->", page_status(r.status_code, str(r.url), page))
            print("  extract_photos ->", len(extract_photos(page, 20)), [u[40:75] for u in extract_photos(page, 20)])
            print("  item-photo testids:", len(set(re.findall(r'data-testid="item-photo-(\d+)--img"', page))))
            print("  InStock:", '"availability":"InStock"' in flat, "| NEXT 404:", 'NEXT_HTTP_ERROR_FALLBACK;404' in page)
            print('  "name":"buy" :', len(re.findall(r'"name":"buy"', flat)), '| "buy" plugin type:', len(re.findall(r'"type":"buy"', flat)))
            for pat in (r'"name":"buyer_item_status"', r'buyer_item_status', r'"can_buy":(true|false)', r'"theme":"SUCCESS"',
                        r'"is_closed":true', r'item_closing_action', r'data-testid="item-status', r'"is_reserved":true',
                        r'"name":"(make_offer|ask_seller|buy)"'):
                ctx = around(flat, pat)
                print(f"  [{pat}] x{len(re.findall(pat, flat))}", ("| " + ctx[0][:520]) if ctx else "")


asyncio.run(main())
