"""Test de bout en bout contre le vrai Vinted : le moteur du bot tourne N secondes.

Rapporte le nombre d'annonces détectées, leur âge estimé au moment de l'alerte, le débit
et les blocages. Sert en CI à vérifier que l'API répond toujours au format attendu.
Usage : python scripts/probe_vinted.py [durée_en_secondes]
"""
import asyncio
import logging
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vinted_bot.config import Config, Search  # noqa: E402
from vinted_bot.monitor import Monitor  # noqa: E402
from vinted_bot.store import Store  # noqa: E402


class Recorder:
    def __init__(self):
        self.items = []

    async def send(self, item, deal, search):
        self.items.append((time.time(), item, search.name))

    async def close(self):
        pass


async def main(duration: float) -> int:
    searches = [
        Search(name="tout le catalogue", weight=2),
        Search(name="nike", params={"search_text": "nike"}),
        Search(name="vestes Nike/adidas < 30", params={"attribute_ids[brand]": "53,14", "price_to": "30"}),
        # Description et avis du vendeur lus sur la page de l'annonce avant l'alerte.
        Search(name="ps5 vérifiée", params={"search_text": "ps5"}, exclude_desc=["paypal"], min_reviews=1),
    ]
    cfg = Config(searches=searches, request_gap=1.0, per_page=48, stats_every=0, extra_photos=False)
    rec = Recorder()
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "probe.sqlite3"))
        mon = Monitor(cfg, store, [rec])
        task = asyncio.create_task(mon.run())
        await asyncio.sleep(duration)
        summary, status = mon.summary(), mon.status()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        store.close()

    print("\n=== RÉSULTAT ===")
    print("Stats :", summary)
    print("Horloge :", f"{mon.clock.rate():.0f} numéros/s" if len(mon.clock.samples) > 1 else "pas assez de relevés")
    for key, st in status["searches"].items():
        print(f"  {key}: {st}")
    print(f"Alertes : {len(rec.items)}")
    ages = [t - it.created_ts for t, it, _ in rec.items if it.created_ts]
    if ages:
        q = statistics.quantiles(ages, n=10) if len(ages) >= 10 else sorted(ages)
        print(f"Âge estimé à l'alerte : médiane {statistics.median(ages):.0f} s, déciles {[round(x) for x in q]}")
    for _, it, name in rec.items[:8]:
        print(f"  [{name}] {it.price} {it.currency} | {it.title[:40]!r} | {it.brand} | {it.size} | {it.status}"
              f" | {it.url}")
    checked = [it for _, it, name in rec.items if name == "ps5 vérifiée"]
    st = status["searches"].get("ps5 vérifiée", {})
    print(f"Vérification sur la page : {len(checked)} alerte(s), {st.get('checked_out', 0)} écartée(s)"
          f" (dernière raison : {st.get('checked_out_last')})")
    for it in checked[:6]:
        print(f"  avis={it.seller_reviews} note={it.seller_rating} vérifiée={it.checked} "
              f"description={len(it.description or '')} car. | {it.title[:40]!r}")
    by_search = {}
    for _, _, name in rec.items:
        by_search[name] = by_search.get(name, 0) + 1
    print("Par recherche :", by_search)
    lus = sum(1 for it in checked if it.seller_reviews is not None) + st.get("checked_out", 0)
    print("Avis lus sur de vraies annonces :", lus)
    ok = len(rec.items) > 0 and status["blocked"] == 0
    print("VERDICT :", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titres avec emojis, console Windows
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(message)s")
    sys.exit(asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 90)))
