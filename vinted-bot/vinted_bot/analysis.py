"""Analyse des recherches et recommandations, à partir des alertes déjà enregistrées (aucune
requête en plus).

Principe : une vraie bonne affaire part en moins de 20 minutes. Pour chaque alerte, le bot sait
si elle est partie en moins de 20 min (vendue ou réservée par quelqu'un d'autre que toi), ou si
elle était encore en vente au bout de 20 min. Une nouvelle annonce est conseillée quand la
plupart des annonces du MÊME modèle, à un prix voisin, sont parties en moins de 20 min.

Le modèle compte : une PS5 Pro à 400 € n'est pas comparable à une PS5 Digital à 400 €.
Il est lu dans le titre (PS5 Pro, PS5 Digital, PS5 avec lecteur) ; sans modèle reconnu,
l'annonce est comparée aux autres annonces de la même recherche.

Limite : la vente est d'abord soupçonnée quand l'annonce disparaît des résultats, puis confirmée
sur sa page (une disparition est souvent une suppression). Sur une recherche très large (des
centaines d'annonces par heure), les annonces sortent des résultats avant d'être vendues et les
ventes sont sous-comptées. Les chiffres sont fiables pour les recherches précises.
"""
from __future__ import annotations

import bisect
import heapq
import re
import statistics
import time
import unicodedata

QUICK = 20 * 60      # « partie vite » : en moins de 20 minutes après l'alerte
MIN_ALERTS = 5       # en dessous, pas de verdict
NEIGHBOURS = 10      # annonces comparées, les plus proches en prix
MIN_EVIDENCE = 4     # au moins 4 annonces comparables dont on connaît l'issue
QUICK_SHARE = 0.5    # conseillée si au moins la moitié est partie en moins de 20 min
PRICE_BELOW = 0.85   # comparables : de 15 % moins cher…
PRICE_ABOVE = 1.25   # … à 25 % plus cher (si plus cher part vite, moins cher partira aussi)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


_PS5 = re.compile(r"\bps\s?5\b|playstation\s?5\b")
_PRO = re.compile(r"\bpro\b")
_DIGITAL = re.compile(r"digital|numerique|sans lecteur|discless|disc\s?less")


def product_model(title: str) -> str | None:
    """Modèle lu dans le titre, pour comparer ce qui est comparable (None : pas reconnu)."""
    t = _norm(title)
    if _PS5.search(t):
        if _PRO.search(t):
            return "PS5 Pro"
        if _DIGITAL.search(t):
            return "PS5 Digital"
        return "PS5 avec lecteur"
    return None


def outcome(alert: dict, now: float) -> bool | None:
    """True : partie en moins de 20 min ; False : encore en vente à 20 min ; None : on ne sait pas
    (trop récente, supprimée, disparue sans confirmation, ou réservée par toi)."""
    if alert.get("reserve") == "ok":
        return None  # c'est toi qui l'as prise : ne dit rien du marché
    after, state = alert.get("availability_after_s"), alert.get("availability")
    if state in ("sold", "reserved") and after is not None:
        return after <= QUICK
    if now - (alert.get("detected_ts") or now) < QUICK:
        return None
    if state == "available" or (after is not None and after > QUICK):
        return False  # encore là à 20 min (partie plus tard, ou pas partie du tout)
    return None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def samples(alerts: list[dict], now: float | None = None) -> list[dict]:
    """Alertes dont on connaît l'issue à 20 min : {id, key, model, price, quick}."""
    now = now or time.time()
    out = []
    for a in alerts:
        if a.get("price") is None:
            continue
        o = outcome(a, now)
        if o is not None:
            out.append({"id": a.get("id"), "key": a.get("search_key"), "model": product_model(a.get("title") or ""),
                        "price": float(a["price"]), "quick": o})
    return out


def quick_evidence(pool: list[dict], key: str | None, title: str, price: float | None,
                   exclude_id=None) -> dict | None:
    """Parmi les annonces comparables (même modèle, prix voisin), combien sont parties en moins
    de 20 min. None si pas assez de données."""
    if price is None or price <= 0:
        return None
    model = product_model(title)
    same = [s for s in pool if s["id"] != exclude_id
            and (s["model"] == model if model else s["key"] == key and s["model"] is None)
            and PRICE_BELOW * price <= s["price"] <= PRICE_ABOVE * price]
    near = sorted(same, key=lambda s: abs(s["price"] - price))[:NEIGHBOURS]
    if len(near) < MIN_EVIDENCE:
        return None
    quick = sum(1 for s in near if s["quick"])
    return {"model": model, "n": len(near), "quick": quick, "share": quick / len(near),
            "lo": min(s["price"] for s in near), "hi": max(s["price"] for s in near)}


def recommend_reason(pool: list[dict], key: str | None, title: str, price: float | None,
                     exclude_id=None) -> str | None:
    ev = quick_evidence(pool, key, title, price, exclude_id)
    if not ev or ev["share"] < QUICK_SHARE:
        return None
    what = ev["model"] or "annonces de ta recherche"
    return (f"⚡ Part vite à ce prix : {ev['quick']} {what} sur {ev['n']} entre {ev['lo']:.0f} et "
            f"{ev['hi']:.0f} € sont parties en moins de 20 min")


def buy_below(pool: list[dict], key: str | None, model: str | None) -> float | None:
    """Prix le plus haut auquel une annonce de ce modèle serait conseillée (repère d'achat).
    Testé de 5 € en 5 € : des milliers d'alertes restent instantanées."""
    group = sorted((s for s in pool if (s["model"] == model if model else s["key"] == key and s["model"] is None)),
                   key=lambda s: s["price"])
    prices = [s["price"] for s in group]
    best = None
    for p in sorted({round(x / 5) * 5 for x in prices if x > 0}):
        window = group[bisect.bisect_left(prices, PRICE_BELOW * p):bisect.bisect_right(prices, PRICE_ABOVE * p)]
        near = heapq.nsmallest(NEIGHBOURS, window, key=lambda s: abs(s["price"] - p))
        if len(near) >= MIN_EVIDENCE and sum(1 for s in near if s["quick"]) / len(near) >= QUICK_SHARE:
            best = p
    return best


def _row(rid: str, name: str, items: list[dict], now: float, pool: list[dict], key: str,
         model: str | None) -> dict:
    first = min(a["detected_ts"] for a in items)
    hours = max(1.0, (now - first) / 3600)
    outcomes = [o for a in items if (o := outcome(a, now)) is not None]
    sold = [a for a in items if a.get("availability") in ("sold", "reserved")
            and a.get("availability_after_s") is not None and a.get("reserve") != "ok"]
    quick = [a for a in sold if a["availability_after_s"] <= QUICK]
    quick_rate = sum(outcomes) / len(outcomes) if outcomes else None
    margins = [a["margin"] for a in items if a.get("margin") is not None]
    row = {
        "id": rid, "name": name, "alerts": len(items), "model": model,
        "per_day": round(len(items) / hours * 24, 1),
        "median_price": _median([a["price"] for a in items if a.get("price") is not None]),
        "sold": len(sold),
        "median_sale_s": _median([a["availability_after_s"] for a in sold]),
        "quick_rate": round(quick_rate, 3) if quick_rate is not None else None,
        "quick_price": _median([a["price"] for a in quick if a.get("price") is not None]),
        "quick_count": len(quick),
        "buy_below": buy_below(pool, key, model),
        "median_margin": _median(margins),
        "positive_margins": sum(1 for m in margins if m > 0) if margins else None,
    }
    if len(items) < MIN_ALERTS or quick_rate is None:
        row["verdict"] = "Trop tôt pour conclure : laisse tourner quelques jours"
    elif quick_rate >= 0.2:
        row["verdict"] = "🔥 Des affaires partent en moins de 20 min : il y a de l'argent ici, sois le plus rapide"
    elif quick_rate >= 0.05:
        row["verdict"] = "👍 Quelques affaires : vise les prix sous « Achète sous »"
    else:
        row["verdict"] = "🐢 Rien ne part en moins de 20 min : peu d'affaires, ou recherche trop large pour mesurer"
    return row


def analyze(alerts: list[dict], searches: list[dict], now: float | None = None) -> list[dict]:
    now = now or time.time()
    by_key: dict[str, list[dict]] = {}
    for a in alerts:
        by_key.setdefault(a.get("search_key") or a.get("search"), []).append(a)
    pool = samples(alerts, now)
    rows = []
    for s in searches:
        items = by_key.get(s["id"], [])
        if not items:
            rows.append({"id": s["id"], "name": s["name"], "alerts": 0, "verdict": "Pas encore de données"})
            continue
        row = _row(s["id"], s["name"], items, now, pool, s["id"], None)
        # Une ligne par modèle reconnu (PS5 Pro, Digital, avec lecteur) : prix et vitesse diffèrent.
        by_model: dict[str, list[dict]] = {}
        for a in items:
            if m := product_model(a.get("title") or ""):
                by_model.setdefault(m, []).append(a)
        row["models"] = [_row(s["id"], m, group, now, pool, s["id"], m)
                         for m, group in sorted(by_model.items(), key=lambda kv: -len(kv[1]))]
        if len(by_model) >= 2:  # prix de modèles différents mélangés : sans signification
            row["median_price"] = row["quick_price"] = None
        rows.append(row)
    rows.sort(key=lambda r: (r.get("quick_rate") is None, -(r.get("quick_rate") or 0)))
    return rows
