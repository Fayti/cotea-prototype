"""Interface web locale : http://127.0.0.1:8765, ouverte automatiquement dans le navigateur.

Le serveur n'écoute que sur la machine elle-même. Les requêtes qui modifient quelque chose
exigent l'en-tête X-VB, qu'une page web tierce ne peut pas envoyer sans autorisation CORS :
un site malveillant ouvert dans le navigateur ne peut donc pas piloter le bot.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import tempfile
import time
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

from aiohttp import web

from .analysis import analyze, price_warning, recommend_reason, samples
from .market import looks_like_packaging
from .buyer import VintedWindow
from .market import DEFAULT_SEGMENTS, MarketWatch, radar_bands, radar_report, report as market_report, segment_params
from .client import VintedClient
from .config import Search, config_from_dict, parse_vinted_url
from .filters import margin
from .models import Deal, Item
from .monitor import Monitor
from .products import product_model
from .notifier import TelegramNotifier, build_notifiers, sample_alert, telegram_detect
from .store import Store

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_PORT = 8765
PORT_ATTEMPTS = 20  # 8765 à 8784 : d'autres applications locales peuvent occuper le port
DEFAULT_SETTINGS = {
    "running": True,
    "general": {"request_gap": 1.0, "proxies": [], "use_direct": True, "extra_photos": True, "track_status": True},
    "deals": {"threshold": 0.30},
    "telegram": {"token": "", "chat_id": ""},
    "search": [],
    "watch": {"enabled": False, "segments": [], "radar": {"enabled": False, "min_price": 200}},
    # Réservation automatique : au plus `per_day` par 24 h ; `history` = moments des dernières.
    "auto_reserve": {"per_day": 2, "history": []},
}
AUTO_PAY_WAIT = 600.0  # après une réservation automatique, 10 min pour payer avant la suivante


class LogBuffer(logging.Handler):
    """Garde les derniers messages du journal pour les afficher dans l'interface."""

    def __init__(self, size: int = 200):
        super().__init__(level=logging.INFO)
        self.records: deque[dict] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({"t": record.created, "level": record.levelname, "msg": record.getMessage()})


LOGS = LogBuffer()


def alert_payload(item: Item, deal, search) -> dict:
    return {
        "id": item.id, "search": search.name, "search_key": search.key,
        "title": item.title, "price": item.price, "total_price": item.total_price,
        "currency": item.currency, "brand": item.brand, "size": item.size, "status": item.status,
        "url": item.url, "photo": item.photo_url, "seller": item.seller,
        "created_ts": item.created_ts, "age_estimated": item.age_estimated, "detected_ts": time.time(),
        "deal": deal.is_deal, "discount": deal.discount, "median": deal.median,
        "resale": search.resale_price, "margin": margin(item, search),
        # Photos : la recherche n'en donne qu'une ; les suivantes arrivent par mise à jour.
        "photos": item.photo_urls[:3] or ([item.photo_url] if item.photo_url else []),
        # Favoris et vues : valeur à l'alerte, puis mises à jour en direct.
        "likes": item.favourites, "likes_start": item.favourites, "views": item.views,
        # Disponibilité (« status » désigne déjà l'état de l'article : très bon état…).
        "availability": "available", "availability_ts": None, "availability_after_s": None,
        # Vendeur et vérification (recherches avec mots interdits dans la description / avis minimum).
        "seller_reviews": item.seller_reviews, "seller_rating": item.seller_rating, "checked": item.checked,
        "reco_reason": item.reco,
        "model": item.model, "kind": item.kind, "warning": item.warning,
    }


class UiNotifier:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    async def send(self, item, deal, search) -> None:
        self.engine.push_alert(alert_payload(item, deal, search))

    async def photos(self, item_id: int, urls: list[str], handle) -> None:
        self.engine.update_alert(item_id, {"photos": urls})

    async def likes(self, t) -> None:
        self.engine.update_alert(t.item_id, {"likes": t.likes, "views": t.views, "likes_ts": t.likes_ts or time.time()})

    async def recheck(self, t, facts: dict, reason: str | None, handle) -> None:
        patch = {"checked": True, "seller_reviews": facts["reviews"], "seller_rating": facts["rating"]}
        if reason:
            patch["flag"] = reason  # arnaque ou panne repérée après coup
        self.engine.update_alert(t.item_id, patch)

    async def status(self, t, handle) -> None:
        self.engine.update_alert(t.item_id, {
            "availability": t.status, "availability_ts": t.status_ts,
            "availability_after_s": round(t.status_ts - t.alerted_ts) if t.status_ts else None,
        })

    async def close(self) -> None:
        pass


def _float_or_none(value):
    if value in (None, ""):
        return None
    return float(str(value).replace(",", "."))


def _word_list(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [w.strip() for w in value or [] if w and w.strip()]


def normalize_search(body: dict, existing: dict | None = None) -> dict:
    url = (body.get("url") or "").strip()
    try:
        _, params = parse_vinted_url(url)
    except ValueError:
        raise ValueError("Ce lien n'est pas une recherche Vinted. Fais ta recherche sur vinted.fr "
                         "puis copie l'adresse de la page.") from None
    name = (body.get("name") or "").strip() or params.get("search_text") or "Ma recherche"
    return {
        "id": (existing or {}).get("id") or secrets.token_hex(5),
        "name": name[:80],
        "url": url,
        "exclude": _word_list(body.get("exclude")),
        "include_any": _word_list(body.get("include_any")),
        "exclude_desc": _word_list(body.get("exclude_desc")),
        "min_reviews": max(0, int(_float_or_none(body.get("min_reviews")) or 0)) or None,
        "anti_scam": bool(body.get("anti_scam", True)),
        "max_price": _float_or_none(body.get("max_price")),
        "only_deals": bool(body.get("only_deals")),
        "auto_reserve_max": _float_or_none(body.get("auto_reserve_max")),
        "resale_price": _float_or_none(body.get("resale_price")),
        "min_margin": _float_or_none(body.get("min_margin")),
        "weight": min(3, max(1, int(body.get("weight") or 1))),
        "enabled": bool(body.get("enabled", True)),
    }


class Engine:
    """Réglages persistés + cycle de vie du moniteur."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "settings.json"
        self.settings = self._load()
        self.store = Store(str(data_dir / "vinted_bot.sqlite3"))
        self.alerts: deque[dict] = deque(reversed(self.store.recent_alerts()), maxlen=150)
        self.listeners: set[asyncio.Queue] = set()
        self.monitor: Monitor | None = None
        self.task: asyncio.Task | None = None
        self.notifiers: list = []
        self.crash: str | None = None
        self.window = VintedWindow(data_dir, on_change=self._window_changed)
        self.side_tasks: list[asyncio.Task] = []
        self.market: MarketWatch | None = None
        self.market_memory: dict = {}  # garde le suivi de la veille d'un redémarrage du moteur à l'autre
        self.reservations: dict[int, dict] = {}
        self.reco_marks: dict[int, dict] = {}      # annonce -> recommandation décidée avant son affichage
        self._pool_cache: tuple[float, list] = (0.0, [])  # issues à 20 min des alertes passées

    def _load(self) -> dict:
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        with contextlib.suppress(FileNotFoundError, ValueError):
            saved = json.loads(self.path.read_text("utf-8"))
            for key, value in saved.items():
                if isinstance(value, dict) and isinstance(settings.get(key), dict):
                    settings[key].update(value)
                else:
                    settings[key] = value
        # Séparation stricte : « Mes recherches » ne contient que ce que tu as enregistré. Une
        # recherche ajoutée par le bot (ancienne suggestion Herno) part dans la veille.
        mine, moved = [], []
        for s in settings.get("search", []):
            (moved if s.get("origin") == "bot" or s.get("name") == "Herno (revente Vestiaire)" else mine).append(s)
        if moved:
            settings["search"] = mine
            watch = settings.setdefault("watch", {"enabled": False, "segments": []})
            for s in moved:
                if not any(seg.get("query") == s["url"] for seg in watch.setdefault("segments", [])):
                    watch["segments"].append({"id": s["id"], "name": s["name"], "query": s["url"]})
        return settings

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)

    def config(self):
        cfg = config_from_dict(self.settings, require_searches=False)
        cfg.console = False
        cfg.stats_every = 300
        return cfg

    async def start(self) -> None:
        await self.stop()
        self.settings["running"] = True
        self.save()
        cfg = self.config()
        self.crash = None
        watch = self.settings.get("watch") or {}
        segments = list(watch.get("segments") or []) if watch.get("enabled") else []
        radar = watch.get("radar") or {}
        if radar.get("enabled"):
            # Radar : tout le catalogue au-dessus d'un prix, sans recherche de départ.
            segments.extend(radar_bands(radar.get("min_price", 200)))
        if not cfg.searches and not segments:
            return
        self.notifiers = build_notifiers(cfg)
        self.monitor = Monitor(cfg, self.store, [UiNotifier(self), *self.notifiers])
        self.monitor.on_auto_reserve = self.auto_reserve
        self.monitor.recommender = self.assess
        self.monitor.on_blocked = self.blocked
        self.task = asyncio.create_task(self._run(self.monitor))
        if segments:  # veille du marché : mêmes connexions et même rythme que la surveillance
            monitor = self.monitor
            self.market = MarketWatch(
                self.store, monitor.client, segments, memory=self.market_memory, on_pepite=self._pepite,
                on_gone=lambda iid, url, domain: monitor._enqueue_page(iid, url, domain, "confirm"))
            monitor.on_confirm = self.store.market_confirm
            monitor.page_domains = {segment_params(s)[0] for s in segments}
            self.side_tasks.append(asyncio.create_task(self.market.run()))
        for n in self.notifiers:
            if hasattr(n, "listen"):  # boutons « ⚡ Réserver » de Telegram
                self.side_tasks.append(asyncio.create_task(n.listen(self._reserve_from_phone)))

    async def _run(self, monitor: Monitor) -> None:
        try:
            await monitor.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Le moniteur s'est arrêté")
            self.crash = f"{type(e).__name__}: {e}"

    async def stop(self, persist: bool = False) -> None:
        for t in self.side_tasks:
            t.cancel()
        self.side_tasks, self.market = [], None
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        for n in self.notifiers:
            with contextlib.suppress(Exception):
                await n.close()
        self.task, self.monitor, self.notifiers = None, None, []
        if persist:
            self.settings["running"] = False
            self.save()

    async def reload(self) -> None:
        self.save()
        if self.settings.get("running"):
            await self.start()

    def _broadcast(self, message: dict) -> None:
        for q in list(self.listeners):
            q.put_nowait(message)

    def _pepite(self, item, seg_id: str, median: float, gain: float) -> None:
        """Pépite repérée par la veille : alerte immédiate (fil, Telegram), comme une recherche."""
        m = self.monitor
        if m is None or item.id in m._alerted:
            return
        m._alerted[item.id] = None
        seg = next((s for s in self.settings.get("watch", {}).get("segments", []) if s["id"] == seg_id), {})
        search = Search(name=f"💎 Veille · {seg.get('name', seg_id)}", key=f"veille-{seg_id}")
        self.reco_marks[item.id] = {"reco": True, "veille": True, "reco_reason":
                                    f"💎 Veille : se vend ~{median:.0f} € d'habitude, gain estimé +{gain:.0f} €"}
        deal = Deal(median=median, discount=1 - item.price / median, is_deal=True)
        log.info("💎 Pépite : %s à %s € (se vend ~%.0f €, gain estimé %.0f €) %s",
                 item.title, item.price, median, gain, item.url)
        t = m.tracker.add(item.id, search.key, item.url)
        if seg:  # likes en direct : la veille de ce segment, relancée au prix exact
            t.domain, t.params = segment_params(seg)
            t.price, t.likes, t.views = item.price, item.favourites, item.views
        if m.cfg.extra_photos and len(item.photo_urls) < m.cfg.photos_per_alert:
            m._spawn(m._alert_with_photos(item, deal, search), "pépite")
        else:
            m._dispatch(item, deal, search)

    def _window_changed(self) -> None:
        self._broadcast({"type": "window", "window": self.window.state()})

    def _alert_url(self, item_id: int) -> str | None:
        alert = next((a for a in self.alerts if a.get("id") == item_id), None)
        tracked = self.monitor.tracker.items.get(item_id) if self.monitor else None
        return (alert or {}).get("url") or (tracked.url if tracked else None)

    async def _reserve_from_phone(self, item_id: int) -> None:
        url = self._alert_url(item_id) or f"https://www.vinted.fr/items/{item_id}"
        await self.reserve(item_id, url, notify=True)

    async def reserve(self, item_id: int, url: str, auto: bool = False, notify: bool = False) -> dict:
        self._set_reservation(item_id, {"reserve": "pending", "reserve_auto": auto, "reserve_msg": None})
        result = await self.window.reserve(item_id, url, auto=auto)
        if notify or auto:
            # Réservé depuis le téléphone, ou tout seul : le résultat part aussi sur Telegram.
            tracked = self.monitor.tracker.items.get(item_id) if self.monitor else None
            for n in self.notifiers:
                if hasattr(n, "reserved") and (notify or result.get("ok")):
                    handle = tracked.handles.get(type(n).__name__) if tracked else None
                    try:
                        await n.reserved(item_id, result, handle, auto)
                    except Exception as e:
                        log.error("Telegram : résultat de réservation non envoyé (%s)", e)
        if result.get("ok"):
            log.info("Annonce %s réservée en %s s : paie dans la fenêtre Vinted", item_id, result.get("seconds"))
            self._set_reservation(item_id, {"reserve": "ok", "reserve_msg": None})
        else:
            log.warning("Réservation de l'annonce %s impossible : %s", item_id, result.get("reason"))
            self._set_reservation(item_id, {"reserve": "fail", "reserve_msg": result.get("reason")})
        return result

    def _set_reservation(self, item_id: int, patch: dict) -> None:
        self.reservations.setdefault(item_id, {}).update(patch)
        if any(a.get("id") == item_id for a in self.alerts):
            self.update_alert(item_id, patch)
        # Sinon l'alerte n'est pas encore affichée (photos en cours) : push_alert reprendra l'état.

    # --- recommandations ----------------------------------------------------------------

    def sample_pool(self) -> list[dict]:
        """Alertes des 14 derniers jours dont on sait si elles sont parties en moins de 20 min."""
        ts, pool = self._pool_cache
        if time.time() - ts > 120:
            pool = samples(self.store.alerts_since(time.time() - 14 * 86400))
            self._pool_cache = (time.time(), pool)
        return pool

    def recommend(self, title: str, price, search_key, item_id=None, model: str | None = "?") -> str | None:
        """Conseillée = les annonces du même modèle, à ce prix, sont parties en moins de 20 min."""
        if looks_like_packaging(title or ""):
            return None
        if model == "?":
            model = product_model(title or "", None, price)
        return recommend_reason(self.sample_pool(), search_key, model, price, item_id)

    def assess(self, item, search) -> str | None:
        """Pour une alerte de tes recherches : avertissement de prix, puis raison de la conseiller."""
        if self.source_of(search.key) != "mine":
            return None
        item.warning = price_warning(self.sample_pool(), item.model, item.price)
        if item.warning:
            return None  # trop beau pour être vrai : jamais conseillée
        return self.recommend(item.title, item.price, search.key, item.id, item.model)

    def blocked(self, item, search, reason: str) -> None:
        """Annonce écartée par l'anti-arnaque ou les vérifications : gardée pour que tu contrôles."""
        row = {"id": item.id, "ts": time.time(), "search": search.name, "search_key": search.key,
               "title": item.title, "price": item.price, "currency": item.currency, "url": item.url,
               "photo": item.photo_url, "reason": reason}
        self.store.add_blocked(row)
        self._broadcast({"type": "blocked", "blocked": row})

    def auto_refusal(self, item, search) -> str | None:
        """Pourquoi NE PAS réserver cette annonce tout seul, ou None. Réserver bloque l'article et
        ouvre un paiement : seules les affaires sûres et prouvées passent, une à la fois."""
        if not self.window.is_open:
            return "fenêtre Vinted fermée"
        if item.checked is not True:
            return "annonce non vérifiée (description ou avis illisibles)"
        if item.kind == "accessoire":
            return "accessoire ou jeu, pas une console"
        if item.warning:
            return "prix anormalement bas pour ce modèle"
        if (item.seller_reviews or 0) < max(1, search.min_reviews or 0):
            return "vendeur sans avis"
        if not item.reco:
            return "rien ne prouve qu'à ce prix ça part en moins de 20 min (pas 💎 conseillée)"
        now = time.time()
        auto = self.settings.setdefault("auto_reserve", {"per_day": 2, "history": []})
        history = [t for t in auto.get("history", []) if now - t < 86400]
        per_day = int(auto.get("per_day", 2))
        if len(history) >= per_day:
            return f"limite de {per_day} réservation(s) automatique(s) par 24 h atteinte"
        if history and now - history[-1] < AUTO_PAY_WAIT:
            return "une réservation automatique attend déjà ton paiement"
        return None

    def auto_reserve(self, item, search) -> None:
        reason = self.auto_refusal(item, search)
        if reason:
            log.info("Réservation automatique de l'annonce %s : non (%s)", item.id, reason)
            self._set_reservation(item.id, {"reserve": "skip", "reserve_auto": True, "reserve_msg": reason})
            return
        auto, ts = self.settings["auto_reserve"], time.time()
        auto["history"] = [t for t in auto.get("history", []) if ts - t < 86400] + [ts]
        self.save()

        async def run():
            result = await self.reserve(item.id, item.url, auto=True)
            if not result.get("ok"):  # ratée (déjà prise…) : ne compte pas dans la limite
                auto["history"] = [t for t in auto["history"] if t != ts]
                self.save()
        asyncio.create_task(run())

    def source_of(self, search_key) -> str:
        """« mine » : alerte d'une recherche que tu as enregistrée ; « bot » : trouvaille du bot."""
        return "mine" if any(s.get("id") == search_key for s in self.settings.get("search", [])) else "bot"

    def push_alert(self, payload: dict) -> None:
        payload["source"] = self.source_of(payload.get("search_key"))
        payload.update(self.reservations.get(payload.get("id"), {}))
        payload.update(self.reco_marks.pop(payload.get("id"), {}))
        if payload.get("reco_reason"):
            payload["reco"] = True
        self.store.add_alert(payload)
        self.alerts.append(payload)
        self._broadcast({"type": "alert", "alert": payload})

    def update_alert(self, item_id: int, patch: dict) -> None:
        """Complète une alerte déjà affichée : photos supplémentaires, vendue…"""
        for alert in self.alerts:
            if alert.get("id") == item_id:
                alert.update(patch)
        self.store.update_alert(item_id, patch)
        self._broadcast({"type": "update", "id": item_id, "patch": patch})

    def status(self) -> dict:
        active = self.task is not None and not self.task.done()
        return {
            "running": bool(self.settings.get("running")),
            "active": active,
            "crash": self.crash,
            "monitor": self.monitor.status() if self.monitor and active else None,
            "window": self.window.state(),
            "logs": list(LOGS.records)[-80:],
        }


# --- routes -------------------------------------------------------------------

routes = web.RouteTableDef()


def engine_of(request) -> Engine:
    return request.app["engine"]


@routes.get("/")
async def index(request):
    return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@routes.get("/api/whoami")
async def whoami(request):
    """Permet de reconnaître une instance de VintedBot parmi les autres applications locales."""
    return web.json_response({"app": "VintedBot", "nonce": request.app["nonce"]})


@routes.get("/api/state")
async def state(request):
    e = engine_of(request)
    return web.json_response({"settings": e.settings, "status": e.status(), "alerts": list(reversed(e.alerts))})


@routes.get("/api/status")
async def status(request):
    return web.json_response(engine_of(request).status())


@routes.put("/api/settings")
async def put_settings(request):
    e = engine_of(request)
    body = await request.json()
    if "general" in body:
        g = body["general"]
        gap = float(g.get("request_gap", e.settings["general"]["request_gap"]))
        e.settings["general"]["request_gap"] = min(10.0, max(0.3, gap))
        if "proxies" in g:
            e.settings["general"]["proxies"] = _word_list(
                g["proxies"].replace("\n", ",") if isinstance(g["proxies"], str) else g["proxies"])
        for flag in ("use_direct", "extra_photos", "track_status"):
            if flag in g:
                e.settings["general"][flag] = bool(g[flag])
    if "auto_reserve" in body:
        per_day = int(body["auto_reserve"].get("per_day", 2))
        e.settings.setdefault("auto_reserve", {"per_day": 2, "history": []})["per_day"] = min(10, max(0, per_day))
    if "deals" in body:
        threshold = float(body["deals"].get("threshold", 0.3))
        e.settings["deals"]["threshold"] = min(0.9, max(0.05, threshold))
    if "telegram" in body:
        e.settings["telegram"] = {
            "token": str(body["telegram"].get("token", "")).strip(),
            "chat_id": str(body["telegram"].get("chat_id", "")).strip(),
        }
    await e.reload()
    return web.json_response({"settings": e.settings})


@routes.post("/api/searches")
async def add_search(request):
    e = engine_of(request)
    search = normalize_search(await request.json())
    e.settings["search"].append(search)
    await e.reload()
    return web.json_response(search)


@routes.put("/api/searches/{id}")
async def update_search(request):
    e = engine_of(request)
    for i, existing in enumerate(e.settings["search"]):
        if existing["id"] == request.match_info["id"]:
            body = {**existing, **await request.json()}
            e.settings["search"][i] = normalize_search(body, existing)
            await e.reload()
            return web.json_response(e.settings["search"][i])
    raise web.HTTPNotFound()


@routes.delete("/api/searches/{id}")
async def delete_search(request):
    e = engine_of(request)
    e.settings["search"] = [s for s in e.settings["search"] if s["id"] != request.match_info["id"]]
    await e.reload()
    return web.json_response({"ok": True})


@routes.post("/api/start")
async def start(request):
    await engine_of(request).start()
    return web.json_response(engine_of(request).status())


@routes.post("/api/stop")
async def stop(request):
    await engine_of(request).stop(persist=True)
    return web.json_response(engine_of(request).status())


@routes.post("/api/preview")
async def preview(request):
    """Teste un lien de recherche : une requête, quelques résultats."""
    e = engine_of(request)
    body = await request.json()
    try:
        domain, params = parse_vinted_url((body.get("url") or "").strip())
    except ValueError:
        raise ValueError("Ce lien n'est pas une recherche Vinted.") from None
    params.update(order="newest_first", per_page="12", page="1")
    g = e.settings["general"]
    client = VintedClient(domain, proxies=g.get("proxies"), use_direct=g.get("use_direct", True), request_gap=0)
    try:
        raw = await client.search(params)
    finally:
        await client.close()
    items = [Item.from_api(r, domain) for r in raw]
    return web.json_response({
        "count": len(items),
        "items": [{"title": i.title, "price": i.price, "currency": i.currency, "photo": i.photo_url, "url": i.url}
                  for i in items[:6]],
    })


@routes.post("/api/telegram/detect")
async def tg_detect(request):
    body = await request.json()
    username, chat_id = await telegram_detect(body.get("token", ""))
    return web.json_response({"username": username, "chat_id": chat_id})


@routes.post("/api/telegram/test")
async def tg_test(request):
    tg = engine_of(request).settings["telegram"]
    if not tg.get("token") or not tg.get("chat_id"):
        raise ValueError("Enregistre d'abord le token et le chat_id")
    notifier = TelegramNotifier(tg["token"], tg["chat_id"])
    item, deal = sample_alert()
    try:
        await notifier.send(item, deal, config_from_dict({"search": [{"name": "Test"}]}).searches[0])
    finally:
        await notifier.close()
    return web.json_response({"ok": True})


@routes.get("/api/blocked")
async def get_blocked(request):
    return web.json_response(engine_of(request).store.recent_blocked(100))


@routes.get("/api/analysis")
async def get_analysis(request):
    e = engine_of(request)
    alerts = e.store.alerts_since(time.time() - 14 * 86400)
    return web.json_response(analyze(alerts, e.settings["search"]))


@routes.post("/api/vinted-window")
async def open_window(request):
    e = engine_of(request)
    await e.window.open()
    return web.json_response(e.window.state())


@routes.post("/api/reserve/{id}")
async def reserve_item(request):
    e = engine_of(request)
    item_id = int(request.match_info["id"])
    alert = next((a for a in e.alerts if a.get("id") == item_id), None)
    body = await request.json() if request.can_read_body else {}
    url = (alert or {}).get("url") or body.get("url")   # annonce de la veille : lien fourni
    if not url or "vinted." not in url:
        raise web.HTTPNotFound()
    return web.json_response(await e.reserve(item_id, url))


# --- veille du marché ---------------------------------------------------------------------

def _watch(e) -> dict:
    w = e.settings.setdefault("watch", {"enabled": False, "segments": []})
    w.setdefault("radar", {"enabled": False, "min_price": 200})
    return w


@routes.get("/api/market")
async def get_market(request):
    e = engine_of(request)
    w = _watch(e)
    rows = e.store.market_rows(time.time() - 7 * 86400)
    data = market_report(rows, w["segments"])
    radar = w["radar"]
    data["radar"] = radar_report([r for r in rows if r["segment"].startswith("radar")],
                                 float(radar.get("min_price", 200)))
    m = e.market
    ids = [b["id"] for b in radar_bands(radar.get("min_price", 200))]
    windows = [m.window_hours(i) for i in ids] if m else []
    data["radar"].update(
        enabled=bool(radar.get("enabled")), running=m is not None and bool(radar.get("enabled")),
        pages=sum(m.pages.get(i, 0) for i in ids) if m else None,
        per_hour=round(sum(m.rate.get(i, 0) for i in ids), 1) if m and any(i in m.rate for i in ids) else None,
        window_h=min((w for w in windows if w), default=None),
        last_poll=max((m.last_poll.get(i, 0) for i in ids), default=0) or None if m else None,
        error=next((m.errors[i] for i in ids if m.errors.get(i)), None) if m else None)
    data["watch"] = {
        "enabled": bool(w.get("enabled")), "running": e.market is not None,
        "segments": [{**s, "last_poll": (e.market.last_poll.get(s["id"]) if e.market else None),
                      "error": (e.market.errors.get(s["id"]) if e.market else None)} for s in w["segments"]],
    }
    return web.json_response(data)


@routes.put("/api/watch")
async def put_watch(request):
    e = engine_of(request)
    body = await request.json()
    w = _watch(e)
    if "enabled" in body:
        w["enabled"] = bool(body["enabled"])
        if w["enabled"]:
            e.settings["running"] = True
    if "radar" in body:
        r = body["radar"]
        if "min_price" in r:
            w["radar"]["min_price"] = max(20.0, min(5000.0, float(r["min_price"])))
        if "enabled" in r:
            w["radar"]["enabled"] = bool(r["enabled"])
            if w["radar"]["enabled"]:
                e.settings["running"] = True
    await e.reload()
    return web.json_response(w)


@routes.post("/api/watch/segments")
async def add_segment(request):
    e = engine_of(request)
    body = await request.json()
    w = _watch(e)
    if body.get("defaults"):
        known = {s["query"] for s in w["segments"]}
        new = [{"id": secrets.token_hex(5), "name": n, "query": q} for n, q in DEFAULT_SEGMENTS if q not in known]
    else:
        query = (body.get("query") or "").strip()
        if not query:
            raise ValueError("Tape une marque ou un type d'article, ou colle un lien de recherche Vinted")
        if query.startswith("http"):
            parse_vinted_url(query)  # lève une erreur claire si ce n'est pas un lien Vinted
        new = [{"id": secrets.token_hex(5), "name": (body.get("name") or "").strip() or query[:60], "query": query}]
    w["segments"].extend(new)
    w["enabled"] = True
    e.settings["running"] = True
    await e.reload()
    return web.json_response({"added": len(new), "watch": w})


@routes.delete("/api/watch/segments/{id}")
async def delete_segment(request):
    e = engine_of(request)
    w = _watch(e)
    w["segments"] = [s for s in w["segments"] if s["id"] != request.match_info["id"]]
    await e.reload()
    return web.json_response(w)


@routes.delete("/api/alerts")
async def clear_alerts(request):
    e = engine_of(request)
    e.store.clear_alerts()
    e.alerts.clear()
    return web.json_response({"ok": True})


@routes.get("/api/events")
async def events(request):
    """Flux SSE : chaque alerte est poussée à l'interface dès sa détection."""
    e = engine_of(request)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    queue: asyncio.Queue = asyncio.Queue()
    e.listeners.add(queue)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=15)
                await resp.write(f"data: {json.dumps(payload)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        e.listeners.discard(queue)
    return resp


@web.middleware
async def guard(request, handler):
    if request.host.rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
        return web.json_response({"error": "Accès refusé"}, status=403)
    if request.method not in ("GET", "HEAD") and request.headers.get("X-VB") != "1":
        return web.json_response({"error": "Accès refusé"}, status=403)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        log.exception("Erreur API %s", request.path)
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)


async def serve(engine: Engine, port: int) -> tuple[web.AppRunner, str]:
    app = web.Application(middlewares=[guard])
    app["engine"] = engine
    app["nonce"] = secrets.token_hex(8)
    app.add_routes(routes)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        raise
    actual = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{actual}"
    # Sur macOS, une autre application peut partager le port (SO_REUSEADDR) et répondre à
    # notre place : on vérifie que c'est bien nous qui répondons à cette adresse.
    info = await asyncio.to_thread(whoami_at, url)
    if info.get("nonce") != app["nonce"]:
        await runner.cleanup()
        raise OSError(f"{url} répond, mais ce n'est pas cette instance de VintedBot")
    return runner, url


def whoami_at(url: str) -> dict:
    """Ce qui répond à cette adresse locale : {"app": "VintedBot", ...} ou {}."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # jamais via un proxy
    try:
        with opener.open(url + "/api/whoami", timeout=2) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def start_server(engine: Engine, preferred: int) -> tuple[web.AppRunner | None, str]:
    """Démarre l'interface sur le premier port libre à partir de `preferred`.

    Renvoie (None, url) si VintedBot tourne déjà : il suffit alors de rouvrir son interface.
    Un port occupé par une autre application (un autre bot local, par exemple) est sauté.
    """
    for port in range(preferred, preferred + PORT_ATTEMPTS):
        url = f"http://127.0.0.1:{port}"
        try:
            return await serve(engine, port)
        except OSError:
            if (await asyncio.to_thread(whoami_at, url)).get("app") == "VintedBot":
                return None, url
            log.info("Port %s occupé par une autre application, essai du suivant", port)
    return await serve(engine, 0)


def default_data_dir() -> Path:
    return Path.home() / "VintedBot"


async def run_ui(data_dir: Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    logging.getLogger().addHandler(LOGS)
    engine = Engine(data_dir)
    runner, url = await start_server(engine, port)
    if runner is None:
        # Déjà lancé (double-clic une deuxième fois) : on rouvre simplement son interface.
        print(f"VintedBot tourne déjà : ouverture de {url}")
        engine.store.close()
        if open_browser:
            webbrowser.open(url)
        return
    print("=" * 60)
    print(f"  VintedBot est lancé : {url}")
    print("  Laisse cette fenêtre ouverte. La fermer arrête le bot.")
    print(f"  Données : {data_dir}")
    print("=" * 60, flush=True)
    if engine.settings.get("running", True):
        await engine.start()
    if open_browser:
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        engine.window.close()
        await engine.stop()
        await runner.cleanup()


async def selftest() -> None:
    """Vérifie que l'exécutable embarque tout (interface, curl_cffi natif) et répond."""
    import aiohttp
    from curl_cffi import Curl

    Curl()  # charge la bibliothèque native de curl_cffi
    with tempfile.TemporaryDirectory() as tmp:
        engine = Engine(Path(tmp))
        runner, url = await serve(engine, 0)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(url + "/") as r:
                    assert r.status == 200 and "VintedBot" in await r.text(), "page d'accueil"
                async with http.post(url + "/api/searches", headers={"X-VB": "1"},
                                     json={"url": "https://www.vinted.fr/catalog?search_text=test"}) as r:
                    assert r.status == 200, f"ajout de recherche : {r.status}"
                async with http.post(url + "/api/searches", json={"url": "x"}) as r:
                    assert r.status == 403, "protection X-VB"
                async with http.get(url + "/api/state") as r:
                    data = await r.json()
                    assert data["settings"]["search"][0]["name"] == "test", "état"
        finally:
            await engine.stop()
            engine.store.close()
            await runner.cleanup()
    print("SELFTEST OK")
