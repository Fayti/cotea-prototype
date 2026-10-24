"""Annonces préparées par le bot : photos nettoyées + titre, description et prix, gardées sur
ton ordinateur (dossier « annonces » des données du bot) jusqu'à leur envoi en brouillon.
"""
from __future__ import annotations

import base64
import json
import secrets
import shutil
import time
from pathlib import Path

from . import listing

DEFAULT_FIELDS = {"model": None, "condition": "tres_bon", "controllers": 1, "games": "", "box": False,
                  "invoice": False, "warranty": "", "storage": "", "slim": False, "notes": ""}
FIELD_KEYS = set(DEFAULT_FIELDS) | {"title_hint"}
# État de l'annonce -> numéro d'état de Vinted (6 neuf avec étiquette, 1 neuf sans étiquette,
# 2 très bon état, 3 bon état). Tu le vérifies de toute façon avant de publier.
STATUS_IDS = {"neuf": 6, "comme_neuf": 2, "tres_bon": 2, "bon": 3}
PS5_CATALOG = 3025   # Électronique > Jeux vidéo et consoles > Consoles (relevé du 24/10/2026)
SONY_BRAND = 113926  # marque « Sony » (relevé du 24/10/2026)


class Drafts:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _dir(self, draft_id: str) -> Path:
        if not draft_id.isalnum():
            raise ValueError("Annonce inconnue")
        return self.root / draft_id

    def get(self, draft_id: str) -> dict:
        path = self._dir(draft_id) / "annonce.json"
        if not path.exists():
            raise ValueError("Annonce inconnue (supprimée ?)")
        return json.loads(path.read_text("utf-8"))

    def save(self, d: dict) -> dict:
        d["updated"] = time.time()
        folder = self._dir(d["id"])
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "annonce.tmp"
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(folder / "annonce.json")
        return d

    def list(self) -> list[dict]:
        out = []
        for path in self.root.glob("*/annonce.json"):
            try:
                out.append(json.loads(path.read_text("utf-8")))
            except ValueError:
                continue
        return sorted(out, key=lambda d: -d.get("created", 0))

    def delete(self, draft_id: str) -> None:
        folder = self._dir(draft_id)
        if folder.exists():
            shutil.rmtree(folder)

    def photo_path(self, draft_id: str, name: str) -> Path:
        if not (name.endswith(".jpg") and name[:-4].isdigit()):
            raise ValueError("Photo inconnue")
        return self._dir(draft_id) / name

    def photos_b64(self, d: dict) -> list[str]:
        return [base64.b64encode(self.photo_path(d["id"], n).read_bytes()).decode() for n in d["photos"]]

    def create(self, photos: list[bytes], text: str = "", source: str = "ordinateur") -> dict:
        """Nouvelle annonce : photos nettoyées (droites, sans GPS), champs devinés des quelques mots."""
        if not photos:
            raise ValueError("Ajoute au moins une photo")
        draft_id = secrets.token_hex(5)
        folder = self._dir(draft_id)
        folder.mkdir(parents=True)
        names = []
        try:
            for i, data in enumerate(photos[:listing.MAX_PHOTOS], 1):
                name = f"{i:02d}.jpg"
                (folder / name).write_bytes(listing.clean_photo(data))
                names.append(name)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        hints = listing.parse_hints(text)
        fields = {**DEFAULT_FIELDS, **{k: v for k, v in hints.items() if k in FIELD_KEYS}}
        d = {"id": draft_id, "created": time.time(), "source": source, "photos": names, "text": text,
             "fields": fields, "price": hints.get("price"), "price_src": "tes mots" if hints.get("price") else None,
             "title_edited": False, "desc_edited": False, "status": "local", "message": None, "vinted_id": None}
        d.update({k: v for k, v in listing.build(fields).items() if k in ("title", "description")})
        return self.save(d)

    def update(self, draft_id: str, patch: dict) -> dict:
        d = self.get(draft_id)
        if "fields" in patch:
            d["fields"].update({k: v for k, v in patch["fields"].items() if k in FIELD_KEYS})
        if "title" in patch:
            d["title"], d["title_edited"] = str(patch["title"])[:100], True
        if "description" in patch:
            d["description"], d["desc_edited"] = str(patch["description"])[:3000], True
        if "price" in patch:
            d["price"] = float(patch["price"]) if patch["price"] not in (None, "") else None
            d["price_src"] = "ton prix"
        if "photos" in patch:  # nouvel ordre, ou photos retirées
            keep = [n for n in patch["photos"] if n in d["photos"]]
            if not keep:
                raise ValueError("Garde au moins une photo")
            for n in set(d["photos"]) - set(keep):
                self.photo_path(draft_id, n).unlink(missing_ok=True)
            d["photos"] = keep
        if patch.get("regenerate"):
            d["title_edited"] = d["desc_edited"] = False
        built = listing.build(d["fields"])
        if not d["title_edited"]:
            d["title"] = built["title"]
        if not d["desc_edited"]:
            d["description"] = built["description"]
        if d["status"] in ("sent", "prefilled", "error") and set(patch) - {"status"}:
            d["status"], d["message"] = "local", None   # modifiée depuis l'envoi : à renvoyer
        return self.save(d)

    @staticmethod
    def vinted_meta(d: dict) -> dict:
        """Ce qui part dans le brouillon Vinted. Catégorie et marque seulement pour une PS5."""
        f = d["fields"]
        ps5 = bool(f.get("model"))
        return {"title": d["title"], "description": d["description"],
                "price": d["price"], "status_id": STATUS_IDS.get(f.get("condition"), 2),
                "catalog_id": PS5_CATALOG if ps5 else None,
                "brand_id": SONY_BRAND if ps5 else None, "brand": "Sony" if ps5 else None}
