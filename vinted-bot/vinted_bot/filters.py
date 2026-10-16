"""Filtres locaux (ce que l'API ne sait pas filtrer) et calcul de bonne affaire."""
from __future__ import annotations

import re
import unicodedata

from .config import Search
from .models import Deal, Item


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(c for c in text if not unicodedata.combining(c))


def _contains_word(haystack: str, needle: str) -> bool:
    # Début de mot : « abimé » attrape « abîmée », « lot » attrape « lots » mais pas « pilot ».
    # Mot court (3 lettres ou moins) : mot entier, pluriel admis : « pro » attrape « pros »
    # mais pas « propre », « jeu » attrape « jeux ».
    word = _norm(needle).strip()
    tail = r"[sx]?(?!\w)" if len(word) <= 3 else ""
    return re.search(rf"(?<!\w){re.escape(word)}{tail}", haystack) is not None


RESALE_FEES = 0.20   # commission de la plateforme de revente (Vestiaire Collective : ~17 % + 3 %)
SHIPPING = 6.0       # envoi de l'achat sur Vinted (Mondial Relay, Relais Colis…)


def margin(item: Item, search: Search) -> float | None:
    """Gain estimé si l'article est revendu au prix de revente indiqué pour la recherche.

    revente − frais de revente − (prix + protection acheteurs Vinted) − envoi.
    """
    if search.resale_price is None or item.price is None:
        return None
    cost = (item.total_price if item.total_price is not None else item.price) + SHIPPING
    return round(search.resale_price * (1 - RESALE_FEES) - cost, 2)


def accept(item: Item, search: Search) -> tuple[bool, str]:
    """Renvoie (accepté, raison du rejet)."""
    title = _norm(item.title)
    for word in search.exclude:
        if _contains_word(title, word):
            return False, f"mot exclu « {word} »"
    for word in search.exclude_desc:  # interdit dans la description : a fortiori dans le titre
        if _contains_word(title, word):
            return False, f"mot interdit « {word} » dans le titre"
    if search.include_any and not any(_contains_word(title, w) for w in search.include_any):
        return False, "aucun mot requis"
    if item.price is not None:
        if search.max_price is not None and item.price > search.max_price:
            return False, "prix trop haut"
        if search.min_price is not None and item.price < search.min_price:
            return False, "prix trop bas"
    if item.seller.lower() in search.exclude_sellers:
        return False, "vendeur exclu"
    if search.min_margin is not None:
        gain = margin(item, search)
        if gain is not None and gain < search.min_margin:
            return False, f"marge estimée trop faible ({gain:.0f} €)"
    return True, ""


def accept_details(item: Item, search: Search) -> tuple[bool, str]:
    """Après lecture de la page : description et avis du vendeur. Ce qui n'a pas pu être lu
    (None) ne fait pas rejeter l'annonce : elle part avec la mention « non vérifiée »."""
    if item.description is not None:
        text = _norm(item.description)
        for word in search.exclude_desc:
            if _contains_word(text, word):
                return False, f"mot interdit « {word} » dans la description"
    if search.min_reviews and item.seller_reviews is not None and item.seller_reviews < search.min_reviews:
        return False, f"vendeur avec {item.seller_reviews} avis (minimum {search.min_reviews})"
    return True, ""


def evaluate_deal(price: float | None, median: float | None, threshold: float) -> Deal:
    if price is None or not median:
        return Deal(median=median, discount=None, is_deal=False)
    discount = 1 - price / median
    return Deal(median=median, discount=discount, is_deal=discount >= threshold)
