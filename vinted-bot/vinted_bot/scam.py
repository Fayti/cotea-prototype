"""Anti-arnaque : signaux d'arnaque et de panne dans le titre et la description d'une annonce.

Les arnaques sur les consoles suivent toujours le même schéma : faire sortir l'acheteur de
Vinted (WhatsApp, numéro de téléphone, e-mail), payer hors de Vinted (PayPal entre amis,
virement, Lydia…), ou empêcher l'achat direct (« ne pas commander avant message ») pour
ouvrir la discussion. Les mots sont souvent déguisés (« W.h.a.t.s.a.p.p », « wtsp »,
« p@ypal », « 06 12 34 56 78 ») : les règles tolèrent séparateurs et chiffres-lettres.

Une mention niée dans la même phrase (« pas de PayPal », « je ne donne pas mon numéro »,
« PayPal non accepté ») ne compte pas : c'est un vendeur honnête qui prévient.
"""
from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿]")
_LEET = str.maketrans({"@": "a", "4": "a", "0": "o", "1": "l", "3": "e", "5": "s", "$": "s", "€": "e"})


def normalize(text: str) -> str:
    text = _ZERO_WIDTH.sub("", (text or "").lower().replace("’", "'").replace("`", "'"))
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def _fuzzy(word: str) -> str:
    """« paypal » attrape aussi « pay pal », « p.a.y.p.a.l », « pay-pal »."""
    return r"[\W_]{0,2}".join(re.escape(c) for c in word)


SEP = r"[\W_]{0,2}"
# Négation dans la même phrase, juste avant la mention.
_NEG_BEFORE = re.compile(r"\b(pas|aucun|aucune|jamais|sans|non|ni|no|refuse|refusee?s?|interdit|zero|evite\w*)\b")
_NEG_AFTER = re.compile(r"^\W{0,3}(non|pas|refuse|interdit|impossible|exclu|jamais|not)\b")
_CLAUSE = re.compile(r"[.!?;,\n:()]")

# (catégorie, expression, négation possible). Expressions appliquées au texte normalisé
# (minuscules, sans accents) puis « décodé » (@ → a, 0 → o…), sauf téléphone et e-mail.
_RULES = [
    ("contact hors Vinted", rf"\b(w{SEP}h?{SEP}a{SEP}t{{1,2}}{SEP}s?{SEP}'?{SEP}a{{1,2}}{SEP}p{{1,2}}|wh?atsap\w*|wtsp|wsp|wpp|whatsap)\b", True),
    ("contact hors Vinted", rf"\b({_fuzzy('telegram')}|{_fuzzy('snapchat')}|snap|messenger|viber|wechat|signal\s*messenger)\b", True),
    ("contact hors Vinted", r"\b(mon|ma|au|via|par|sur)\s+(num|numero|tel|telephone|portable|phone|insta|instagram)\b", True),
    ("contact hors Vinted", r"\bnumero\b(?!\s+(de\s+|d')?(serie|suivi|commande|facture|colis|modele|lot|reference|article|annonce|produit|cfi|ref))", True),
    ("contact hors Vinted", r"\b(contact|ecri|appel|envoi|joind|texto|sms)\w*[\s-]+(moi[\s-]+|nous[\s-]+)?(au|par|sur|via|en)\s+(sms|tel|mail|e-?mail|whats\w*|snap\w*|insta\w*|telegram)", True),
    ("contact hors Vinted", r"\b(gmail|hotmail|outlook|yahoo|icloud|proton)\b", True),
    ("paiement hors Vinted", rf"\b{_fuzzy('paypal')}\w*", True),
    ("paiement hors Vinted", r"\b(amis?|ami)\s*(et|&|ou)\s*(famille|proches)\b|\bfriends?\s*(and|&|n)\s*family\b", True),
    ("paiement hors Vinted", r"\b(virement|lydia|wero|paylib|western\s*union|mandat\s*cash|neosurf|transcash|bitcoin|crypto|usdt|revolut|iban|coupon\s*pcs|pcs\s*mastercard)\b", True),
    ("paiement hors Vinted", r"\b(hors|en dehors (de|du))\s+(vinted|plateforme|site|appli)\b|\b(sans passer|pas passer|pas)\s+par\s+vinted\b", True),
    ("achat direct refusé", r"\bne\s+(pas|surtout pas)\s+(acheter|commander|payer|reserver|cliquer|valider|faire d'?achat)", False),
    ("achat direct refusé", r"\bn'?\s?(achetez|commandez|payez|reservez)\s+pas\b", False),
    ("achat direct refusé", r"\b(pas|aucun)\s+d'?\s?achats?\s+(direct|immediat|sans)", False),
    ("achat direct refusé", r"\bachats?\s+(direct|immediat)s?\s+(impossible|interdit|refuse|bloque|annule)", False),
    ("achat direct refusé", r"\b(contact\w*|ecri\w*|envoy\w*|envoie\w*|message|mp|dm|demand\w*)[\s-]+(moi[\s-]+|nous[\s-]+)?(un\s+)?(message\s+|mp\s+)?avant\s+(tout\s+|l'|d'|de\s+|toute\s+)?(achat|acheter|commande|commander|payer|paiement|reservation)", False),
    ("achat direct refusé", r"\bavant\s+(tout\s+|toute\s+|l'|d'|de\s+)?(achat|acheter|commande|commander|paiement)\W{0,3}(merci\s+de\s+)?(me\s+)?(contact|message|mp|ecri|envoy|demand)", False),
    ("achat direct refusé", r"\bmessage\s+obligatoire\b|\bmessagerie\s+(vinted\s+)?(bug|beug|ne marche|ne fonctionne|hs)\w*", False),
]
_RULES = [(label, re.compile(rx), neg) for label, rx, neg in _RULES]
_PHONE = re.compile(r"(?<![\d\w])(?:\+\s?33|0033|0)\s*[1-9](?:[\s.\-/]*\d\d){4}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+\s?(@|\(at\)|\[at\]|arobase)\s?[\w-]+\s?\.\s?[a-z]{2,}")

# Consoles en panne, bloquées ou pour pièces.
_DEFECTS = [
    r"\bh\.?s\b", r"\ben panne\b", r"\bpour pieces?\b", r"\bpieces? detachees\b",
    r"\bne s'?\s?allume (pas|plus)\b", r"\bne (fonctionne|marche) (pas|plus)\b", r"\becran noir\b",
    r"\bbann?ie?s?\b", r"\bban (psn|ps|definitif)\b", r"\bbloquee?\b", r"\bsurchauffe\w*\b", r"\bcarte mere\b",
    r"\b(lumiere|led|voyant) bleue? clignot", r"\berreur ce-?\d", r"\ba reparer\b", r"\bpour reparation\b",
    r"\bdefectueu\w*\b", r"\bne lit (pas|plus)\b", r"\bplus d'?\s?image\b",
]
_DEFECTS = [re.compile(rx) for rx in _DEFECTS]


def _negated(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 40):start]
    cut = list(_CLAUSE.finditer(before))
    if cut:
        before = before[cut[-1].end():]
    after = text[end:end + 25]
    return bool(_NEG_BEFORE.search(before)) or bool(_NEG_AFTER.search(after))


def _excerpt(text: str, start: int, end: int) -> str:
    """Le passage en cause, en mots entiers (quelques mots autour)."""
    a, b = max(0, start - 20), min(len(text), end + 20)
    if a > 0:
        a = text.find(" ", a, start) + 1 or start  # ne pas couper le premier mot
    if b < len(text):
        b = text.rfind(" ", end, b) if text.rfind(" ", end, b) > 0 else end
    return re.sub(r"\s+", " ", text[a:b]).strip(" ,.;:")


def scam_reason(text: str) -> str | None:
    """« arnaque probable : contact hors Vinted (« …contacte moi sur whatsapp… ») », ou None."""
    plain = normalize(text)
    for rx, label in ((_PHONE, "numéro de téléphone"), (_EMAIL, "adresse e-mail")):
        for m in rx.finditer(plain):
            if not _negated(plain, m.start(), m.end()):
                return f"arnaque probable : {label} (« {_excerpt(plain, m.start(), m.end())} »)"
    decoded = plain.translate(_LEET)
    for label, rx, negatable in _RULES:
        for m in rx.finditer(decoded):
            if not (negatable and _negated(decoded, m.start(), m.end())):
                return f"arnaque probable : {label} (« {_excerpt(plain, m.start(), m.end())} »)"
    return None


def defect_reason(text: str) -> str | None:
    """« en panne ou bloquée (« …ne s'allume plus… ») », ou None. « Jamais tombée en panne »,
    « non bannie » ne comptent pas."""
    plain = normalize(text)
    for rx in _DEFECTS:
        for m in rx.finditer(plain):
            if not _negated(plain, m.start(), m.end()):
                return f"en panne, bloquée ou pour pièces (« {_excerpt(plain, m.start(), m.end())} »)"
    return None
