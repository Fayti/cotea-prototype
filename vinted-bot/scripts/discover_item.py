"""Sonde temporaire (CI) : comment le site Vinted crée un brouillon (code public du site)."""
import asyncio
import re
from urllib.parse import urljoin

from curl_cffi.requests import AsyncSession

PATTERNS = ["item_upload", "/drafts", "assigned_photos", "temp_uuid", "photo[type]", "upload_session",
            "csrf", "CSRF", "package_size_id", "is_unisex", "status_id", "item_attributes", "isbn"]


def hits(text, pat, n=3, span=260):
    out = []
    for m in re.finditer(re.escape(pat), text):
        out.append(text[max(0, m.start() - span):m.end() + span].replace("\n", " "))
        if len(out) >= n:
            break
    return out


async def main():
    async with AsyncSession(impersonate="chrome", timeout=30, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        pages = {}
        for path in ("/", "/items/new", "/catalog?search_text=ps5"):
            r = await s.get("https://www.vinted.fr" + path)
            pages[path] = r.text
            print("PAGE", path, r.status_code, r.url, len(r.text))
            await asyncio.sleep(3)
        tok = s.cookies.get("access_token_web")
        for path, html in pages.items():
            for pat in ("csrf", "CSRF"):
                for h in hits(html, pat, 2, 120):
                    print("  HTML", path, pat, "::", h[:300])
        chunks = set()
        for html in pages.values():
            for u in re.findall(r'(?:src|href)="([^"]+?\.js)"', html):
                chunks.add(urljoin("https://www.vinted.fr/", u))
            for u in re.findall(r'(/_next/static/[^"\'\s\\]+?\.js)', html):
                chunks.add(urljoin("https://www.vinted.fr/", u))
        print("CHUNKS", len(chunks))
        found = {p: [] for p in PATTERNS}
        more = set()
        for i, u in enumerate(sorted(chunks)):
            try:
                r = await s.get(u)
            except Exception as e:
                print("ERR", u, e)
                continue
            js = r.text
            for m in re.findall(r'(static/chunks/[\w\-./\[\]]+?\.js)', js):
                more.add("https://www.vinted.fr/_next/" + m)
            for p in PATTERNS:
                if p in js and len(found[p]) < 4:
                    found[p].extend((u.rsplit("/", 1)[-1], h) for h in hits(js, p, 2))
        print("MORE CHUNKS", len(more - chunks))
        for i, u in enumerate(sorted(more - chunks)[:400]):
            try:
                r = await s.get(u)
            except Exception:
                continue
            js = r.text
            for p in PATTERNS:
                if p in js and len(found[p]) < 6:
                    found[p].extend((u.rsplit("/", 1)[-1], h) for h in hits(js, p, 2))
        for p, lst in found.items():
            print(f"\n##### {p} : {len(lst)}")
            for name, h in lst[:6]:
                print("  [", name, "]", h[:540])
        auth = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        for api in ("/api/v2/catalog/initializers", "/api/v2/catalogs", "/api/v2/brands?keyword=sony",
                    "/api/v2/item_upload/brands?keyword=sony", "/api/v2/item_statuses", "/api/v2/package_sizes",
                    "/api/v2/item_upload/catalogs"):
            await asyncio.sleep(2)
            r = await s.get("https://www.vinted.fr" + api, headers=auth)
            body = r.text
            print("\nAPI", api, r.status_code, len(body), body[:300].replace("\n", " "))
            for h in hits(body, "Consoles", 2, 200) + hits(body, '"PlayStation"', 1, 200) + hits(body, '"Sony"', 1, 150):
                print("    ", h[:420])


asyncio.run(main())
