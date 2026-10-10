"""Interroge le vrai Vinted et affiche ce qui revient : accès, format des annonces, latence.

Sert en CI à vérifier que l'API répond toujours au format attendu par le bot.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vinted_bot.client import VintedClient  # noqa: E402
from vinted_bot.models import Item  # noqa: E402


async def main() -> int:
    client = VintedClient("vinted.fr", request_gap=1.0)
    params = {"search_text": "nike", "order": "newest_first", "per_page": "10", "page": "1"}
    try:
        for attempt in range(3):
            t0 = time.monotonic()
            raw = await client.search(params)
            rtt = (time.monotonic() - t0) * 1000
            print(f"requête {attempt + 1} : {len(raw)} annonces en {rtt:.0f} ms")
        if not raw:
            print("ÉCHEC : aucune annonce renvoyée")
            return 1
        print("clés d'une annonce :", sorted(raw[0].keys()))
        now = time.time()
        for r in raw[:5]:
            item = Item.from_api(r, "vinted.fr")
            age = f"{now - item.created_ts:.0f} s" if item.created_ts else "?"
            print(f"  #{item.id} {item.price} {item.currency} | {item.title[:40]!r} | photo il y a {age} | {item.url}")
        missing = [k for k in ("id", "title", "price", "url", "photo") if k not in raw[0]]
        print("champs attendus manquants :", missing or "aucun")
        return 1 if missing else 0
    except Exception as e:
        print(f"ÉCHEC : {type(e).__name__}: {e}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
