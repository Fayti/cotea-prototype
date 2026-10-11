"""Sonde temporaire (CI) : adresse ouverte par le bouton « Acheter » (code JS du site)."""
import asyncio
import re

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"
PATS = [r"transaction/buy[^\"'`\s]{0,80}", r"/checkout[^\"'`\s]{0,80}", r"buy/new[^\"'`\s]{0,80}",
        r"source_screen[^\"'`\s]{0,60}", r"item_id\]=[^\"'`\s]{0,40}", r"purchase_id[^\"'`\s]{0,40}"]


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get(WWW + "/catalog")
        await asyncio.sleep(3)
        r = await s.get("https://api.vinted.fr/svc-catalogue/items", params={"order": "newest_first", "per_page": "5"},
                        headers={"Authorization": f"Bearer {s.cookies.get('access_token_web')}", "Accept": "application/json"})
        iid = [i["id"] for i in r.json()["items"] if not i.get("promoted")][0]
        await asyncio.sleep(6)
        page = (await s.get(f"{WWW}/items/{iid}")).text
        chunks = sorted(set(re.findall(r'https://marketplace-web-assets\.vinted\.com/_next/static/chunks/[^"\\]+\.js', page)))
        print("chunks", len(chunks))
        hits = {}
        for url in chunks:
            js = (await s.get(url)).text
            for p in PATS:
                for m in re.finditer(p, js):
                    ctx = js[max(0, m.start() - 160):m.end() + 160].replace("\n", " ")
                    hits.setdefault(m.group(0), (url.rsplit("/", 1)[-1], ctx))
        for k, (u, ctx) in list(hits.items())[:40]:
            print(f"\n== {k}  ({u})\n   {ctx}")


asyncio.run(main())
