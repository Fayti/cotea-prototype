"""Sonde temporaire (CI) : fonctionnement de la réservation (code JS du site Vinted)."""
import asyncio
import re

from curl_cffi.requests import AsyncSession

WWW = "https://www.vinted.fr"
PATS = [r"checkout/build", r"initiateSingleCheckout", r"x-csrf-token", r"X-CSRF-Token", r"csrf_token", r"CSRF_TOKEN",
        r"auth/refresh", r"refresh_token_web", r"x-anon-id", r"X-Anon-Id", r"baseURL", r"/api/v2", r"/web/api/",
        r"purchase_items", r"checkoutUrl", r"CHECKOUT_URL", r'"type":"transaction"', r"order_type", r"access_token_web",
        r"withCredentials", r"oauth/token", r"v_uid"]


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        home = await s.get(WWW + "/catalog")
        print("cookies", sorted(s.cookies.keys()))
        for p in (r"csrf[^,]{0,120}", r"CSRF[^,]{0,120}"):
            print("HTML", p, sorted(set(re.findall(p, home.text)))[:5])
        await asyncio.sleep(3)
        r = await s.get("https://api.vinted.fr/svc-catalogue/items", params={"order": "newest_first", "per_page": "5"},
                        headers={"Authorization": f"Bearer {s.cookies.get('access_token_web')}", "Accept": "application/json"})
        iid = [i["id"] for i in r.json()["items"] if not i.get("promoted")][0]
        await asyncio.sleep(6)
        page = (await s.get(f"{WWW}/items/{iid}")).text
        for p in (r"csrf.{0,100}", r"CSRF.{0,100}"):
            print("ITEM", p, sorted(set(m[:100] for m in re.findall(p, page)))[:6])
        chunks = sorted(set(re.findall(r'https://marketplace-web-assets\.vinted\.com/_next/static/chunks/[^"\\]+\.js', page + home.text)))
        print("chunks", len(chunks))
        seen = {}
        for url in chunks:
            js = (await s.get(url)).text
            for p in PATS:
                for m in list(re.finditer(p, js))[:3]:
                    ctx = js[max(0, m.start() - 350):m.end() + 350].replace("\n", " ")
                    seen.setdefault(p, []).append((url.rsplit("/", 1)[-1], ctx))
        for p in PATS:
            hits = seen.get(p, [])
            print(f"\n######## {p} x{len(hits)}")
            for u, ctx in hits[:3]:
                print(f"--- {u}\n{ctx}")


asyncio.run(main())
