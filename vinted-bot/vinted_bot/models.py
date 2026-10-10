"""Normalisation des annonces renvoyées par l'API catalogue."""
from __future__ import annotations

from dataclasses import dataclass


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
    created_ts: int | None
    favourites: int

    @classmethod
    def from_api(cls, raw: dict, domain: str) -> "Item":
        price, currency = _amount(raw.get("price"))
        currency = currency or raw.get("currency", "")
        total, _ = _amount(raw.get("total_item_price"))
        photo = raw.get("photo") or {}
        hires = photo.get("high_resolution") or {}
        url = raw.get("url") or ""
        if not url and raw.get("path"):
            url = f"https://www.{domain}{raw['path']}"
        if not url:
            url = f"https://www.{domain}/items/{raw['id']}"
        return cls(
            id=int(raw["id"]),
            title=raw.get("title") or "",
            price=price,
            currency=currency or "EUR",
            total_price=total,
            brand=raw.get("brand_title") or "",
            size=raw.get("size_title") or "",
            status=raw.get("status") or "",
            url=url,
            photo_url=photo.get("url") or "",
            seller=(raw.get("user") or {}).get("login") or "",
            created_ts=hires.get("timestamp"),
            favourites=int(raw.get("favourite_count") or 0),
        )


@dataclass
class Deal:
    median: float | None
    discount: float | None  # 0.4 = 40 % sous la médiane
    is_deal: bool
