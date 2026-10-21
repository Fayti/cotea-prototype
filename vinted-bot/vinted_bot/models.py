"""Normalisation des annonces renvoyées par l'API catalogue."""
from __future__ import annotations

from dataclasses import dataclass, field


def _amount(value) -> tuple[float | None, str]:
    """L'API a renvoyé le prix soit en chaîne ("12.0"), soit en objet {amount, currency_code}."""
    if value is None:
        return None, ""
    if isinstance(value, dict):
        try:
            return float(value.get("amount")), value.get("currency_code", "")
        except (TypeError, ValueError):
            return None, value.get("currency_code", "")
    try:
        return float(value), ""
    except (TypeError, ValueError):
        return None, ""


@dataclass
class Item:
    id: int
    title: str
    price: float | None
    currency: str
    total_price: float | None
    brand: str
    size: str
    status: str
    url: str
    photo_url: str
    seller: str
    created_ts: float | None
    favourites: int
    promoted: bool = False
    age_estimated: bool = False
    photo_urls: list[str] = field(default_factory=list)  # photo principale d'abord
    views: int = 0
    seller_id: int | None = None
    # Lus sur la page de l'annonce quand la recherche l'exige (mots interdits, avis minimum).
    description: str | None = None
    seller_reviews: int | None = None   # nombre d'évaluations du vendeur
    seller_rating: float | None = None  # note de 0 à 1 (1 = 5 étoiles)
    checked: bool | None = None         # None : rien à vérifier ; False : vérification impossible
    reco: str | None = None             # pourquoi le bot la conseille
    kind: str | None = None             # PS5 : « console » ou « accessoire »
    model: str | None = None            # PS5 : « PS5 Pro », « PS5 Digital », « PS5 avec lecteur »
    warning: str | None = None          # ex. prix anormalement bas pour ce modèle

    @classmethod
    def from_api(cls, raw: dict, domain: str) -> "Item":
        price, currency = _amount(raw.get("price"))
        currency = currency or raw.get("currency", "")
        total, _ = _amount(raw.get("total_item_price"))
        photo = raw.get("photo") or {}
        hires = photo.get("high_resolution") or {}
        title = raw.get("title") or ""
        url = raw.get("url") or raw.get("path") or f"/items/{raw['id']}"
        if url.startswith("/"):
            url = f"https://www.{domain}{url}"  # l'API svc-catalogue renvoie des URL relatives
        # API svc-catalogue (septembre 2026) : marque en première ligne, « taille · état » en
        # seconde. Sans marque, la première ligne reprend le titre.
        box = raw.get("item_box") or {}
        brand = raw.get("brand_title") or ""
        size = raw.get("size_title") or ""
        status = raw.get("status") or ""
        if box:
            first = box.get("first_line") or ""
            brand = brand or ("" if first == title else first)
            parts = [p.strip() for p in (box.get("second_line") or "").split("·") if p.strip()]
            if parts:
                status = status or parts[-1]
                size = size or " · ".join(parts[:-1])
        photo_urls = []
        for p in [photo, *(raw.get("photos") or [])]:
            u = (p or {}).get("url") or (p or {}).get("full_size_url")
            if u and u not in photo_urls:
                photo_urls.append(u)
        return cls(
            id=int(raw["id"]),
            title=title,
            price=price,
            currency=currency or "EUR",
            total_price=total,
            brand=brand,
            size=size,
            status=status,
            url=url,
            photo_url=photo.get("url") or "",
            seller=(raw.get("user") or {}).get("login") or "",
            seller_id=(raw.get("user") or {}).get("id"),
            created_ts=hires.get("timestamp"),  # absent depuis septembre 2026 : estimé par IdClock
            favourites=int(raw.get("favourite_count") or 0),
            views=int(raw.get("view_count") or 0),
            promoted=bool(raw.get("promoted")) or raw.get("content_source") == "catalog_promoted_items",
            photo_urls=photo_urls,
        )


@dataclass
class Deal:
    median: float | None
    discount: float | None  # 0.4 = 40 % sous la médiane
    is_deal: bool
