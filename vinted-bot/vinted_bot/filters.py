"""Filtres locaux (ce que l'API ne sait pas filtrer) et calcul de bonne affaire."""
from __future__ import annotations

import re
import unicodedata

from .config import Search
from .models import Deal, Item


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _contains_word(haystack: str, needle: str) -> bool:
    # Début de mot : « abimé » attrape « abîmée », « lot » attrape « lots » mais pas « pilot ».
    return re.search(rf"(?<!\w){re.escape(_norm(needle))}", haystack) is not None


def accept(item: Item, search: Search) -> tuple[bool, str]:
    """Renvoie (accepté, raison du rejet)."""
    title = _norm(item.title)
    for word in search.exclude:
        if _contains_word(title, word):
            return False, f"mot exclu « {word} »"
    if search.include_any and not any(_contains_word(title, w) for w in search.include_any):
        return False, "aucun mot requis"
    if item.price is not None:
        if search.max_price is not None and item.price > search.max_price:
            return False, "prix trop haut"
        if search.min_price is not None and item.price < search.min_price:
            return False, "prix trop bas"
    if item.seller.lower() in search.exclude_sellers:
        return False, "vendeur exclu"
    return True, ""


def evaluate_deal(price: float | None, median: float | None, threshold: float) -> Deal:
    if price is None or not median:
        return Deal(median=median, discount=None, is_deal=False)
    discount = 1 - price / median
    return Deal(median=median, discount=discount, is_deal=discount >= threshold)
