"""Sonde temporaire (CI) : bouton de paiement (code du site) et suivi des likes par recherche ciblée."""
import asyncio
import re

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"
API = "https://api.vinted.fr/svc-catalogue/items"


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get(WWW + "/catalog")
        auth = {"Authorization": f"Bearer {s.cookies.get('access_token_web')}", "Accept": "application/json"}
        await asyncio.sleep(2)
        r = await s.get(API, params={"order": "newest_first", "per_page": "30"}, headers=auth)
        items = [i for i in r.json()["items"] if not i.get("promoted")][:6]
        # 1) Retrouver une annonce précise : son titre + son prix exact
        for it in items:
            await asyncio.sleep(2)
            price = it["price"]["amount"]
            q = {"search_text": it["title"][:80], "price_from": price, "price_to": price,
                 "order": "newest_first", "per_page": "20"}
            rr = await s.get(API, params=q, headers=auth)
            got = rr.json().get("items", []) if rr.status_code == 200 else []
            ids = [g["id"] for g in got]
            print(f"titre={it['title'][:40]!r} prix={price} -> HTTP {rr.status_code}, {len(ids)} résultats, "
                  f"trouvée={'oui pos %d' % ids.index(it['id']) if it['id'] in ids else 'non'}", flush=True)
        # 2) Page de paiement : morceaux de code chargés, puis attributs data-testid liés au paiement
        pages = []
        for url in (WWW + "/checkout?purchase_id=1&order_type=transaction", WWW + f"/items/{items[0]['id']}"):
            await asyncio.sleep(3)
            p = await s.get(url)
            print("page", url[-60:], "HTTP", p.status_code, "->", str(p.url)[-60:], len(p.text), flush=True)
            pages.append(p.text)
        chunks = sorted(set(re.findall(r'https://marketplace-web-assets\.vinted\.com/_next/static/chunks/[^"\\]+\.js',
                                       "".join(pages))))
        print("morceaux", len(chunks))
        ids, texts = set(), set()
        for url in chunks:
            js = (await s.get(url)).text
            for m in re.finditer(r'testId:"([^"]{0,80})"|"data-testid":"([^"]{0,80})"|data-testid="([^"]{0,80})"', js):
                tid = next(g for g in m.groups() if g)
                if re.search(r"pay|checkout|purchase|buy|order-summary|confirm", tid, re.I):
                    ids.add(tid)
            for m in re.finditer(r'.{0,60}(single-checkout|checkout\.(?:pay|actions|submit)[\w.]*).{0,60}', js):
                texts.add(m.group(1))
        print("testids paiement :", sorted(ids)[:80])
        print("clés :", sorted(texts)[:40])


asyncio.run(main())
