"""Reconnaissance des PS5 : console ou accessoire/jeu, et modèle (Pro, Digital, avec lecteur).

Le modèle compte pour juger un prix : une PS5 Pro à 400 € n'est pas comparable à une PS5
Digital à 400 €. Le titre décide d'abord ; s'il ne dit pas le modèle (« PS5 slim 1 To »), la
description le précise souvent (« édition numérique », « avec lecteur »).
"""
from __future__ import annotations

import re

from .scam import normalize

_PS5 = re.compile(r"\b(ps\s?5|play\s?station\s?5|playstation5)(?=\b|pro|slim)")
_PRO = re.compile(r"\b(ps\s?5|play\s?station\s?5)\s*pro\b|\bpro\b")
_PAD_PRO = re.compile(r"\b(manettes?|controllers?|dualsense)\W+(\w+\W+){0,2}pro\b")  # « PS5 + manette pro »
_DIGITAL = re.compile(r"digital|numerique|sans lecteur|discless|disc\s?less|dematerialise")
_DISC = re.compile(r"avec lecteur|lecteur (blu-?ray|disque|cd)|edition standard|version standard|standard edition|disc edition")
_DESC_PRO = re.compile(r"\b(ps\s?5|play\s?station\s?5)\s*pro\b")
_DESC_DIGITAL = re.compile(r"\b(edition|version)\s+(digitale?|numerique)\b|\bdigital edition\b|\bsans lecteur\b")
# Mots qui disent « console » (le reste du titre peut citer manettes et jeux livrés avec).
_CONSOLE = re.compile(r"\bconsoles?\b|\bslim\b|\bfat\b|\bpro\b|digital|numerique|standard|edition|\b(825|1|2)\s?(go|to|tb)\b"
                      r"|\bcfi-?\s?\d|\bpack\b|\bbundle\b|\bavec\s+(manette|jeu|jeux|boite)")
# Placés AVANT « PS5 », ces mots désignent un accessoire ou un jeu (« Manette PS5 », « Jeu PS5 »).
_ACCESSORY = re.compile(r"\b(manettes?|jeux?|games?|coques?|facades?|faceplates?|support|stand|station|casques?|"
                        r"cables?|chargeurs?|skins?|stickers?|housses?|sacoches?|lecteur|disque|ssd|volant|camera|"
                        r"telecommande|remote|portal|vr2?|dualsense|edge|pulse|boites?|cartons?|ventilateur|"
                        r"carte|abonnement|cle|figurine|affiche|poster|manuel|notice)\b")


def is_ps5(title: str) -> bool:
    return bool(_PS5.search(normalize(title)))


def product_kind(title: str, price: float | None = None) -> str | None:
    """« console », « accessoire » (manette, jeu, coque…) ou None si ce n'est pas une PS5."""
    t = normalize(title)
    m = _PS5.search(t)
    if not m:
        return None
    if _ACCESSORY.search(t[:m.start()]) and not re.search(r"\bconsoles?\b", t):
        return "accessoire"   # « Manette PS5 », « Jeu PS5 Spider-Man », « Coque PS5 slim »
    if _CONSOLE.search(t):
        return "console"
    if price is not None and price < 100:
        return "accessoire"   # « Spider-Man 2 PS5 » à 30 € : un jeu
    return "console"


def product_model(title: str, description: str | None = None, price: float | None = None) -> str | None:
    """« PS5 Pro », « PS5 Digital », « PS5 avec lecteur » (consoles seulement), ou None."""
    if product_kind(title, price) != "console":
        return None
    t = normalize(title)
    if _PRO.search(t) and (_DESC_PRO.search(t) or not _PAD_PRO.search(t)):
        return "PS5 Pro"
    if _DIGITAL.search(t):
        return "PS5 Digital"
    if _DISC.search(t):
        return "PS5 avec lecteur"
    d = normalize(description or "")
    if _DESC_PRO.search(d):
        return "PS5 Pro"
    if _DESC_DIGITAL.search(d):
        return "PS5 Digital"
    return "PS5 avec lecteur"
