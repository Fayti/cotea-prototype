"""Canaux d'alerte : console, Telegram (le plus rapide sur mobile), Discord."""
from __future__ import annotations

import html
import logging
import time

from curl_cffi.requests import AsyncSession

from .config import Search
from .models import Deal, Item
from .tracking import AVAILABLE, DELETED, GONE, RESERVED, SOLD, STATUS_LABELS

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
    approx = "~" if item.age_estimated else ""
    return f"{approx}{seconds} s" if seconds < 120 else f"{approx}{seconds // 60} min"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"


def status_banner(t) -> str:
    """Bandeau d'état d'une annonce suivie, en texte simple."""
    after = f" {_duration((t.status_ts or time.time()) - t.alerted_ts)} après l'alerte" if t.status_ts else ""
    return {
        SOLD: f"❌ VENDUE{after}",
        GONE: f"⚠️ PLUS DISPONIBLE{after} (sans doute vendue)",
        RESERVED: f"⏸ RÉSERVÉE{after}",
        DELETED: f"🗑 SUPPRIMÉE{after}",
        AVAILABLE: "✅ De nouveau disponible",
    }.get(t.status, STATUS_LABELS.get(t.status, t.status))


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

    async def status(self, t, handle) -> None:
        print(f"{status_banner(t)} : {t.url}", flush=True)

    async def close(self) -> None:
        pass


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = AsyncSession(timeout=10)

    @staticmethod
    def _message(text: str, url: str, photo: str) -> dict:
        payload = {
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[{"text": "Ouvrir sur Vinted", "url": url}]]},
        }
        if photo:
            payload["link_preview_options"] = {"url": photo, "prefer_large_media": True, "show_above_text": True}
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        return payload

    async def _call(self, method: str, payload: dict) -> dict:
        resp = await self.http.post(f"{self.api}/{method}", json={"chat_id": self.chat_id, **payload})
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram {method} HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("result") or {}

    async def send(self, item: Item, deal: Deal, search: Search) -> dict:
        # sendMessage part immédiatement ; sendPhoto ferait d'abord télécharger l'image
        # par Telegram. La photo arrive en aperçu de lien, au-dessus du texte.
        text = html.escape(format_text(item, deal, search).rsplit("\n", 1)[0])
        result = await self._call("sendMessage", self._message(text, item.url, item.photo_url))
        # Référence gardée pour ajouter les photos et signaler la vente sur ce même message.
        return {"message_id": result.get("message_id"), "text": text, "url": item.url, "photo": item.photo_url}

    async def photos(self, item_id: int, urls: list[str], handle: dict | None) -> None:
        """Album des photos, en réponse silencieuse à l'alerte : le téléphone ne sonne qu'une fois."""
        payload = {"media": [{"type": "photo", "media": u} for u in urls[:10]], "disable_notification": True}
        if handle and handle.get("message_id"):
            payload["reply_parameters"] = {"message_id": handle["message_id"], "allow_sending_without_reply": True}
        await self._call("sendMediaGroup", payload)

    async def status(self, t, handle: dict | None) -> None:
        """Bandeau d'état ajouté en tête du message d'alerte d'origine (sans nouvelle sonnerie)."""
        if not handle or not handle.get("message_id"):
            return
        text = f"<b>{html.escape(status_banner(t))}</b>\n{handle['text']}"
        await self._call("editMessageText", {"message_id": handle["message_id"],
                                             **self._message(text, handle["url"], handle.get("photo", ""))})

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


def build_notifiers(cfg) -> list:
    notifiers = []
    if cfg.console:
        notifiers.append(ConsoleNotifier())
    if cfg.telegram_token and cfg.telegram_chat_id:
        notifiers.append(TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id))
    if cfg.discord_webhook:
        notifiers.append(DiscordNotifier(cfg.discord_webhook))
    return notifiers


def sample_alert() -> tuple[Item, Deal]:
    item = Item(
        id=1, title="Test — Nike Air Max 90", price=25.0, currency="EUR", total_price=27.95,
        brand="Nike", size="42", status="Très bon état", url="https://www.vinted.fr/",
        photo_url="", seller="vinted_bot", created_ts=None, favourites=0,
    )
    return item, Deal(median=45.0, discount=1 - 25 / 45, is_deal=True)


async def telegram_detect(token: str) -> tuple[str, str | None]:
    """Renvoie (nom du bot, chat_id du dernier message reçu par le bot ou None)."""
    token = token.strip()
    async with AsyncSession(timeout=10) as http:
        me = await http.get(f"https://api.telegram.org/bot{token}/getMe")
        if me.status_code != 200:
            raise ValueError("Token Telegram invalide : recopie-le depuis le message de @BotFather")
        username = me.json()["result"]["username"]
        updates = await http.get(f"https://api.telegram.org/bot{token}/getUpdates")
        chat_id = None
        if updates.status_code == 200:
            for update in updates.json().get("result", []):
                message = update.get("message") or update.get("my_chat_member") or {}
                if chat := message.get("chat"):
                    chat_id = str(chat["id"])
        return username, chat_id
