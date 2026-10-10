"""Canaux d'alerte : console, Telegram (le plus rapide sur mobile), Discord."""
from __future__ import annotations

import html
import logging
import time

from curl_cffi.requests import AsyncSession

from .config import Search
from .models import Deal, Item

log = logging.getLogger(__name__)


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "?"
    symbol = {"EUR": "€", "GBP": "£", "USD": "$", "PLN": "zł", "CZK": "Kč"}.get(currency, currency)
    return f"{value:.2f} {symbol}"


def _age(item: Item) -> str:
    if not item.created_ts:
        return ""
    seconds = max(0, int(time.time() - item.created_ts))
    return f"{seconds} s" if seconds < 120 else f"{seconds // 60} min"


def headline(item: Item, deal: Deal) -> str:
    if deal.is_deal and deal.discount is not None:
        return f"🔥 -{deal.discount:.0%} vs médiane"
    return "🆕 Nouvelle annonce"


def format_text(item: Item, deal: Deal, search: Search) -> str:
    lines = [f"{headline(item, deal)} · {search.name}", item.title]
    price = _money(item.price, item.currency)
    if item.total_price is not None:
        price += f" ({_money(item.total_price, item.currency)} avec frais)"
    if deal.median is not None:
        price += f" · médiane {_money(deal.median, item.currency)}"
    lines.append(f"💶 {price}")
    details = " · ".join(x for x in (item.brand, item.size, item.status) if x)
    if details:
        lines.append(f"🏷 {details}")
    meta = f"👤 {item.seller}" if item.seller else ""
    if age := _age(item):
        meta += f" · ⏱ publiée il y a {age}"
    if meta:
        lines.append(meta.strip(" ·"))
    lines.append(item.url)
    return "\n".join(lines)


class ConsoleNotifier:
    async def send(self, item: Item, deal: Deal, search: Search) -> None:
        print("\n" + format_text(item, deal, search), flush=True)

    async def close(self) -> None:
        pass


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = AsyncSession(timeout=10)

    async def send(self, item: Item, deal: Deal, search: Search) -> None:
        # sendMessage part immédiatement ; sendPhoto ferait d'abord télécharger l'image
        # par Telegram. La photo arrive en aperçu de lien, au-dessus du texte.
        text = html.escape(format_text(item, deal, search).rsplit("\n", 1)[0])
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[{"text": "Ouvrir sur Vinted", "url": item.url}]]},
        }
        if item.photo_url:
            payload["link_preview_options"] = {
                "url": item.photo_url, "prefer_large_media": True, "show_above_text": True,
            }
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        resp = await self.http.post(f"{self.api}/sendMessage", json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")

    async def close(self) -> None:
        await self.http.close()


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.url = webhook_url
        self.http = AsyncSession(timeout=10)

    async def send(self, item: Item, deal: Deal, search: Search) -> None:
        body = format_text(item, deal, search).split("\n")
        embed = {
            "title": item.title[:256] or "Annonce",
            "url": item.url,
            "description": "\n".join(body[2:-1]),
            "color": 0xE8590C if deal.is_deal else 0x09B1BA,
            "author": {"name": f"{headline(item, deal)} · {search.name}"[:256]},
        }
        if item.photo_url:
            embed["image"] = {"url": item.photo_url}
        resp = await self.http.post(self.url, json={"embeds": [embed]})
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"Discord HTTP {resp.status_code}: {resp.text[:200]}")

    async def close(self) -> None:
        await self.http.close()
