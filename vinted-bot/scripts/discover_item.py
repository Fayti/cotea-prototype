"""Sonde temporaire (CI) : photos en pleine taille d'une annonce, et leur téléchargement."""
import asyncio
import re

from curl_cffi.requests import AsyncSession

API = "https://api.vinted.fr/svc-catalogue/items"


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get("https://www.vinted.fr/catalog")
        auth = {"Authorization": f"Bearer {s.cookies.get('access_token_web')}", "Accept": "application/json"}
        r = await s.get(API, params={"search_text": "ps5", "order": "newest_first", "per_page": "20", "page": "1"}, headers=auth)
        items = [i for i in r.json().get("items") or [] if not i.get("promoted")]
        for it in items[:2]:
            await asyncio.sleep(10)
            url = it.get("url") or it.get("path")
            url = "https://www.vinted.fr" + url if url.startswith("/") else url
            r = await s.get(url, headers={"Referer": "https://www.vinted.fr/catalog"})
            page = r.text
            flat = page.replace('\\"', '"').replace("\\u0026", "&")
            print("\n=== PAGE", it["id"], r.status_code, len(page), it.get("title"))
            tags = re.findall(r'<img[^>]+data-testid="item-photo-(\d+)--img"[^>]*>', page)
            print("gallery tags:", len(tags))
            for m in re.finditer(r'<img[^>]+data-testid="item-photo-\d+--img"[^>]*>', page):
                print("  TAG", m.group(0)[:400])
                break
            fulls = sorted(set(re.findall(r'https://images1\.vinted\.net/tc/[^"\\\s]+', flat)))
            print("tc urls:", len(fulls))
            for u in fulls[:3]:
                print("  ", u[:200])
            for key in ("full_size_url", "high_resolution", '"photos":['):
                idx = flat.find(key)
                print(f"[{key}] count={flat.count(key)}", flat[max(0, idx - 200):idx + 500].replace("\n", " ")[:700] if idx >= 0 else "")
            f800 = sorted(set(re.findall(r'https://images1\.vinted\.net/t/[^"\\\s]+?/f800/[^"\\\s]+', flat)))
            print("f800 urls:", len(f800))
            # téléchargement
            for u in (fulls[:1] + f800[:1]):
                rr = await s.get(u, headers={"Referer": url})
                print("  DOWNLOAD", rr.status_code, rr.headers.get("content-type"), len(rr.content), u[:120])
            pb = re.findall(r'https://images1\.vinted\.net/t/[^"\\\s]+?/(\d+x\d+|f\d+)/', flat)
            from collections import Counter
            print("formats:", Counter(pb).most_common(8))


asyncio.run(main())
