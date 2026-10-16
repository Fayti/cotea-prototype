"""Canaux d'alerte : console, Telegram (le plus rapide sur mobile), Discord."""
from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import logging
import time

from curl_cffi import CurlMime
from curl_cffi.requests import AsyncSession

from .config import Search
from .filters import margin
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
    if item.reco:
        return "💎 CONSEILLÉE"
    if deal.is_deal and deal.discount is not None:
        return f"🔥 -{deal.discount:.0%} vs médiane"
    return "🆕 Nouvelle annonce"


def format_text(item: Item, deal: Deal, search: Search) -> str:
    lines = [f"{headline(item, deal)} · {search.name}", item.title]
    if item.reco:
        lines.append(item.reco)
    price = _money(item.price, item.currency)
    if item.total_price is not None:
        price += f" ({_money(item.total_price, item.currency)} avec frais)"
    if deal.median is not None:
        price += f" · médiane {_money(deal.median, item.currency)}"
    lines.append(f"💶 {price}")
    gain = margin(item, search)
    if gain is not None:
        lines.append(f"💰 Marge estimée {gain:+.0f} € si revendue {search.resale_price:.0f} €")
    details = " · ".join(x for x in (item.brand, item.size, item.status) if x)
    if details:
        lines.append(f"🏷 {details}")
    meta = f"👤 {item.seller}" if item.seller else ""
    if item.seller_reviews is not None:
        stars = f"{item.seller_rating * 5:.1f}".replace(".", ",") + " ★, " if item.seller_rating and item.seller_reviews else ""
        meta += f" ({stars}{item.seller_reviews} avis)"
    if age := _age(item):
        meta += f" · ⏱ publiée il y a {age}"
    if meta:
        lines.append(meta.strip(" ·"))
    if item.checked is False:
        lines.append("⚠️ Description ou avis non vérifiés (Vinted a freiné la lecture des pages) : regarde avant d'acheter")
    lines.append(item.url)
    return "\n".join(lines)


class ConsoleNotifier:
    async def send(self, item: Item, deal: Deal, search: Search) -> None:
        print("\n" + format_text(item, deal, search), flush=True)

    async def status(self, t, handle) -> None:
        print(f"{status_banner(t)} : {t.url}", flush=True)

    async def close(self) -> None:
        pass


def _cover(img, w: int, h: int):
    from PIL import ImageOps
    return ImageOps.fit(img, (w, h), method=3, centering=(0.5, 0.4))


def collage(images: list) -> bytes:
    """Les photos réunies en une seule image : la principale à gauche, les autres à droite."""
    from PIL import Image

    W, H, GAP = 1200, 800, 6
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    if len(images) == 2:
        half = (W - GAP) // 2
        canvas.paste(_cover(images[0], half, H), (0, 0))
        canvas.paste(_cover(images[1], half, H), (half + GAP, 0))
    else:
        left, right, cell = 796, W - 796 - GAP, (H - GAP) // 2
        canvas.paste(_cover(images[0], left, H), (0, 0))
        canvas.paste(_cover(images[1], right, cell), (left + GAP, 0))
        canvas.paste(_cover(images[2], right, cell), (left + GAP, cell + GAP))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=85)
    return out.getvalue()


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.http = AsyncSession(timeout=10)

    @staticmethod
    def _keyboard(item_id: int | None, url: str, reserve: bool = True) -> dict:
        row = [{"text": "Voir sur Vinted", "url": url}]
        if reserve and item_id:
            # Le bouton réserve l'annonce sur l'ordinateur, dans la fenêtre Vinted connectée.
            row.insert(0, {"text": "⚡ Réserver", "callback_data": f"r:{item_id}"})
        return {"inline_keyboard": [row]}

    @classmethod
    def _message(cls, text: str, url: str, photo: str, item_id: int | None = None, reserve: bool = True) -> dict:
        payload = {"text": text, "parse_mode": "HTML", "reply_markup": cls._keyboard(item_id, url, reserve)}
        if photo:
            payload["link_preview_options"] = {"url": photo, "prefer_large_media": True, "show_above_text": True}
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        return payload

    async def _call(self, method: str, payload: dict, timeout: float | None = None):
        body = payload if "chat_id" in payload or method in ("getUpdates", "answerCallbackQuery") \
            else {"chat_id": self.chat_id, **payload}
        resp = await self.http.post(f"{self.api}/{method}", json=body, timeout=timeout or 10)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram {method} HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("result") or {}

    async def _collage(self, urls: list[str]) -> bytes | None:
        from PIL import Image

        async def fetch(u):
            r = await self.http.get(u)
            return r.content if r.status_code == 200 else None

        images = []
        for raw in await asyncio.gather(*(fetch(u) for u in urls[:3]), return_exceptions=True):
            if isinstance(raw, bytes):
                with contextlib.suppress(Exception):
                    images.append(Image.open(io.BytesIO(raw)).convert("RGB"))
        return await asyncio.to_thread(collage, images) if len(images) >= 2 else None

    async def send(self, item: Item, deal: Deal, search: Search) -> dict:
        text = html.escape(format_text(item, deal, search).rsplit("\n", 1)[0])
        handle = {"text": text, "url": item.url, "item_id": item.id}
        image = None
        if len(item.photo_urls) >= 2:
            try:
                image = await self._collage(item.photo_urls)
            except Exception as e:
                log.debug("Assemblage des photos impossible : %s", e)
        if image:
            # Un seul message : les 3 photos réunies en une image, le texte et les boutons dessous.
            form = CurlMime()
            form.addpart(name="photo", content_type="image/jpeg", filename="photos.jpg", data=image)
            resp = await self.http.post(f"{self.api}/sendPhoto", multipart=form, timeout=20, data={
                "chat_id": self.chat_id, "caption": text, "parse_mode": "HTML",
                "reply_markup": json.dumps(self._keyboard(item.id, item.url))})
            if resp.status_code == 200:
                result = resp.json().get("result") or {}
                return {**handle, "message_id": result.get("message_id"), "caption": True}
            log.warning("Telegram sendPhoto HTTP %s : %s", resp.status_code, resp.text[:200])
        # Une seule photo : sendMessage part immédiatement, la photo arrive en aperçu de lien.
        result = await self._call("sendMessage", self._message(text, item.url, item.photo_url, item.id))
        return {**handle, "message_id": result.get("message_id"), "photo": item.photo_url}

    async def status(self, t, handle: dict | None) -> None:
        """Bandeau d'état ajouté en tête du message d'alerte d'origine (sans nouvelle sonnerie)."""
        if not handle or not handle.get("message_id"):
            return
        text = f"<b>{html.escape(status_banner(t))}</b>\n{handle['text']}"
        still_on_sale = t.status == AVAILABLE
        if handle.get("album"):  # alertes envoyées par une version précédente
            await self._call("editMessageCaption", {"message_id": handle["message_id"], "caption": text,
                                                    "parse_mode": "HTML"})
        elif handle.get("caption"):
            await self._call("editMessageCaption", {
                "message_id": handle["message_id"], "caption": text, "parse_mode": "HTML",
                "reply_markup": self._keyboard(handle.get("item_id"), handle["url"], still_on_sale)})
        else:
            await self._call("editMessageText", {"message_id": handle["message_id"], **self._message(
                text, handle["url"], handle.get("photo", ""), handle.get("item_id"), still_on_sale)})

    async def reserved(self, item_id: int, result: dict, handle: dict | None, auto: bool) -> None:
        """Résultat d'une réservation, en réponse au message d'alerte. Sonne : il faut payer vite."""
        if result.get("ok"):
            took = f" en {result['seconds']} s" if result.get("seconds") else ""
            text = (f"✅ <b>Réservée pour toi{' automatiquement' if auto else ''}</b>{took}.\n"
                    "Paie vite : sans validation, la réservation expire. Le paiement est aussi ouvert "
                    "sur ton ordinateur, dans la fenêtre Vinted.")
            markup = {"inline_keyboard": [[{"text": "💳 Payer maintenant", "url": result["checkout_url"]}]]} \
                if result.get("checkout_url") else None
        else:
            text = f"❌ <b>Pas réservée</b> : {html.escape(result.get('reason') or 'erreur inconnue')}"
            markup = None
        payload = {"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if markup:
            payload["reply_markup"] = markup
        if handle and handle.get("message_id"):
            payload["reply_parameters"] = {"message_id": handle["message_id"], "allow_sending_without_reply": True}
        await self._call("sendMessage", payload)

    async def listen(self, on_reserve) -> None:
        """Boutons « ⚡ Réserver » : réception des appuis (long polling) depuis le téléphone.

        Les appuis faits pendant que le bot était arrêté sont ignorés : réserver une annonce
        vieille de plusieurs heures n'a plus de sens.
        """
        offset, warned = None, False
        with contextlib.suppress(Exception):
            last = await self._call("getUpdates", {"offset": -1, "timeout": 0})
            if last:
                offset = last[-1]["update_id"] + 1
        while True:
            try:
                resp = await self.http.post(f"{self.api}/getUpdates", timeout=40, json={
                    "offset": offset, "timeout": 25, "allowed_updates": ["callback_query"]})
                if resp.status_code == 409:
                    if not warned:
                        log.warning("Boutons Telegram inactifs : un autre programme lit déjà les messages de ce "
                                    "bot Telegram (utilise un bot Telegram dédié à VintedBot)")
                        warned = True
                    await asyncio.sleep(60)
                    continue
                for update in resp.json().get("result") or []:
                    offset = update["update_id"] + 1
                    query = update.get("callback_query") or {}
                    message = query.get("message") or {}
                    data = query.get("data") or ""
                    if not data.startswith("r:"):
                        continue
                    if str((message.get("chat") or {}).get("id")) != self.chat_id:
                        continue  # seul ton propre compte Telegram peut réserver
                    with contextlib.suppress(Exception):
                        await self._call("answerCallbackQuery", {
                            "callback_query_id": query["id"], "text": "⏳ Réservation sur ton ordinateur…"})
                    asyncio.get_running_loop().create_task(on_reserve(int(data[2:])))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("Lecture des boutons Telegram : %s", e)
                await asyncio.sleep(5)

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
