"""Sonde temporaire (CI) : temps de réponse de l'API selon le nombre d'annonces demandées."""
import asyncio
import statistics
import time

from curl_cffi.requests import AsyncSession

API = "https://api.vinted.fr/svc-catalogue/items"


async def main():
    async with AsyncSession(impersonate="chrome", timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"}) as s:
        await s.get("https://www.vinted.fr/catalog")
        auth = {"Authorization": f"Bearer {s.cookies.get('access_token_web')}", "Accept": "application/json"}
        times = {20: [], 48: [], 96: []}
        sizes = {20: [], 48: [], 96: []}
        for rnd in range(12):
            for per in (20, 48, 96):
                for q in ("nike", "levis"):
                    await asyncio.sleep(1.2)
                    t0 = time.perf_counter()
                    r = await s.get(API, params={"search_text": q, "order": "newest_first", "per_page": str(per), "page": "1"},
                                    headers=auth)
                    dt = (time.perf_counter() - t0) * 1000
                    if r.status_code == 200:
                        times[per].append(dt)
                        sizes[per].append(len(r.content))
                    else:
                        print("HTTP", r.status_code, per)
        for per in times:
            t = sorted(times[per])
            print(f"per_page={per}: n={len(t)} médiane={statistics.median(t):.0f} ms p90={t[int(len(t)*0.9)-1]:.0f} ms "
                  f"taille≈{statistics.median(sizes[per])/1024:.0f} Ko")


asyncio.run(main())
