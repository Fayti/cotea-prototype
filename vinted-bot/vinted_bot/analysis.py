"""Analyse des recherches à partir des alertes déjà enregistrées (aucune requête en plus).

Principe : une annonce vendue très vite était sous-évaluée. La part d'annonces parties en
moins d'une heure mesure donc, recherche par recherche, à quel point le marché laisse passer
des affaires, et le prix de ces ventes rapides donne le vrai prix d'achat à viser.

Limite : la vente est détectée quand l'annonce disparaît des résultats alors que des annonces
plus anciennes y sont encore. Sur une recherche très large (des centaines d'annonces par
heure), les annonces sortent de la première page avant d'être vendues et les ventes sont
sous-comptées. Les chiffres sont fiables pour les recherches précises.
"""
from __future__ import annotations

import statistics
import time

QUICK = 3600.0       # « vendue vite » : dans l'heure qui suit l'alerte
MIN_ALERTS = 5       # en dessous, pas de verdict


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def analyze(alerts: list[dict], searches: list[dict], now: float | None = None) -> list[dict]:
    now = now or time.time()
    by_key: dict[str, list[dict]] = {}
    for a in alerts:
        by_key.setdefault(a.get("search_key") or a.get("search"), []).append(a)
    rows = []
    for s in searches:
        items = by_key.get(s["id"], [])
        if not items:
            rows.append({"id": s["id"], "name": s["name"], "alerts": 0, "verdict": "Pas encore de données"})
            continue
        first = min(a["detected_ts"] for a in items)
        hours = max(1.0, (now - first) / 3600)
        judged = [a for a in items if now - a["detected_ts"] >= QUICK]  # ont eu une heure pour partir
        sold = [a for a in items if a.get("availability") in ("sold", "gone", "reserved")
                and a.get("availability_after_s") is not None]
        quick = [a for a in sold if a["availability_after_s"] <= QUICK]
        quick_rate = len([a for a in quick if a in judged]) / len(judged) if judged else None
        margins = [a["margin"] for a in items if a.get("margin") is not None]
        row = {
            "id": s["id"], "name": s["name"], "alerts": len(items),
            "per_day": round(len(items) / hours * 24, 1),
            "median_price": _median([a["price"] for a in items if a.get("price") is not None]),
            "sold": len(sold),
            "median_sale_s": _median([a["availability_after_s"] for a in sold]),
            "quick_rate": round(quick_rate, 3) if quick_rate is not None else None,
            "quick_price": _median([a["price"] for a in quick if a.get("price") is not None]),
            "median_margin": _median(margins),
            "positive_margins": sum(1 for m in margins if m > 0) if margins else None,
        }
        if len(items) < MIN_ALERTS or quick_rate is None:
            row["verdict"] = "Trop tôt pour conclure : laisse tourner quelques jours"
        elif quick_rate >= 0.2:
            row["verdict"] = "🔥 Des affaires partent très vite : il y a de l'argent ici, sois le plus rapide"
        elif quick_rate >= 0.05:
            row["verdict"] = "👍 Quelques affaires : surveille les prix sous la colonne « prix des ventes rapides »"
        else:
            row["verdict"] = "🐢 Ça part lentement : peu d'affaires, ou recherche trop large pour mesurer"
        rows.append(row)
    rows.sort(key=lambda r: (r.get("quick_rate") is None, -(r.get("quick_rate") or 0)))
    return rows
