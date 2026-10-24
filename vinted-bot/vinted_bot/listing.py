"""Annonce automatique : à partir de tes photos (et de quelques mots), un titre, une description
et un prix prêts à publier, enregistrés en brouillon sur ton compte Vinted.

Les photos sont remises droites (orientation de l'iPhone), réduites à 2000 px au plus et
nettoyées de leurs métadonnées : une photo d'iPhone contient souvent la position GPS de
l'endroit où elle a été prise (ton domicile), qui ne doit pas partir avec l'annonce.
"""
from __future__ import annotations

import io
import re

from .scam import normalize

MAX_SIDE = 2000
MAX_PHOTOS = 20

CONDITIONS = {  # clé -> (libellé de l'annonce, phrase de la description)
    "neuf": ("Neuf", "neuve, jamais utilisée"),
    "comme_neuf": ("Comme neuf", "comme neuve, très peu servie"),
    "tres_bon": ("Très bon état", "en très bon état, parfaitement fonctionnelle"),
    "bon": ("Bon état", "en bon état, fonctionne parfaitement (quelques traces d'usage)"),
}
MODEL_NAMES = {"PS5 Pro": "PlayStation 5 Pro", "PS5 Digital": "PlayStation 5 Digital",
               "PS5 avec lecteur": "PlayStation 5 avec lecteur"}

try:  # photos HEIC de l'iPhone (envoyées depuis le Mac, l'AirDrop ou Telegram en fichier)
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # bibliothèque absente : seules les photos JPEG/PNG/WebP sont acceptées
    pillow_heif = None


def clean_photo(data: bytes) -> bytes:
    """JPEG droit, 2000 px au plus, sans métadonnées (ni GPS, ni modèle de téléphone)."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        hint = " (photo HEIC : réessaie en JPEG)" if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1") else ""
        raise ValueError(f"Photo illisible{hint}") from e
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)   # sans exif= : aucune métadonnée recopiée
    return out.getvalue()


def parse_hints(text: str) -> dict:
    """Ce que disent quelques mots libres : « ps5 pro 2 manettes boite facture 520 € très bon état »."""
    t = normalize(text or "")
    hints: dict = {}
    if re.search(r"\bps\s?5|play\s?station\s?5", t):   # ici, c'est forcément la console que tu vends
        if re.search(r"\bpro\b|ps\s?5\s?pro", t) and not re.search(r"manettes?\W+(\w+\W+){0,2}pro\b", t):
            hints["model"] = "PS5 Pro"
        elif re.search(r"digital|numerique|sans lecteur", t):
            hints["model"] = "PS5 Digital"
        else:
            hints["model"] = "PS5 avec lecteur"
    if m := re.search(r"\b(\d{1,2})\s*manettes?\b", t):
        hints["controllers"] = int(m.group(1))
    elif re.search(r"\bmanettes?\b", t):
        hints["controllers"] = 1
    if re.search(r"\bsans (sa |la )?boite\b", t):
        hints["box"] = False
    elif re.search(r"\bboite\b", t):
        hints["box"] = True
    if re.search(r"\bsans facture\b", t):
        hints["invoice"] = False
    elif re.search(r"\bfacture\b", t):
        hints["invoice"] = True
    if re.search(r"\bslim\b", t):
        hints["slim"] = True
    if m := re.search(r"\b(825\s?go|1\s?to|2\s?to)\b", t):
        hints["storage"] = m.group(1).replace(" ", "").replace("go", " Go").replace("to", " To")
    for key, words in (("comme_neuf", r"\bcomme neuve?\b"),
                       ("neuf", r"\bneuve?\b|\bsous blister\b|\bjamais (ouverte?|utilisee?)\b"),
                       ("tres_bon", r"\btres bon etat\b|\btbe\b"),
                       ("bon", r"\bbon etat\b")):
        if re.search(words, t):
            hints["condition"] = key
            break
    if m := re.search(r"garantie\s*(jusqu'?\s?(en|au)?\s*)?([0-9/]{4,10}|\w+ 20\d\d)", t):
        hints["warranty"] = m.group(3)
    prices = re.findall(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*(?:€|e\b|eur|euros?\b)", t)
    if prices:
        hints["price"] = float(prices[-1].replace(",", "."))
    if m := re.search(r"\bjeux?\s*:\s*([^\n]+)", text or "", re.I):
        hints["games"] = m.group(1).strip()
    return hints


def build(fields: dict) -> dict:
    """Titre et description à partir des champs (modèle, état, manettes, jeux, boîte, facture…)."""
    model = fields.get("model") or ""
    condition = fields.get("condition") or "tres_bon"
    cond_label, cond_text = CONDITIONS.get(condition, CONDITIONS["tres_bon"])
    controllers = int(fields.get("controllers") or 0)
    storage = (fields.get("storage") or "").strip()
    slim = " Slim" if fields.get("slim") and model != "PS5 Pro" else ""
    games = (fields.get("games") or "").strip()
    box, invoice = bool(fields.get("box")), bool(fields.get("invoice"))
    warranty = (fields.get("warranty") or "").strip()
    notes = (fields.get("notes") or "").strip()

    if model:
        bits = [f"{model}{slim}" + (f" {storage}" if storage else "")]
        if controllers >= 2:
            bits.append(f"+ {controllers} manettes")
        if games:
            bits.append("+ jeux")
        extras = [w for w, on in (("boîte", box), ("facture", invoice)) if on]
        title = " ".join(bits) + (f", {' et '.join(extras)}" if extras else "")
    else:
        title = (fields.get("title_hint") or "").strip() or "Article"

    lines = []
    if model:
        name = MODEL_NAMES.get(model, model)
        lines.append(f"{name}{slim}" + (f" {storage}" if storage else "") + f" {cond_text}.")
        content = ["la console"]
        if controllers:
            content.append(f"{controllers} manette{'s' if controllers > 1 else ''} DualSense")
        content.append("les câbles HDMI et d'alimentation")
        if box:
            content.append("la boîte d'origine")
        if invoice:
            content.append("la facture")
        lines.append("Vendue avec " + ", ".join(content[:-1]) + " et " + content[-1] + ".")
        if games:
            lines.append(f"Jeux inclus : {games}.")
        if warranty:
            lines.append(f"Encore sous garantie : jusqu'en {warranty}.")
    if notes:
        lines.append(notes)
    lines.append("Envoi rapide et très bien protégé.")
    return {"title": title[:100], "description": "\n".join(lines), "condition_label": cond_label}
