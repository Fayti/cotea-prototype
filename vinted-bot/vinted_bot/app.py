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

from .client import VintedClient
from .config import config_from_dict, parse_vinted_url
from .models import Item
from .monitor import Monitor
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
}


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
        # Photos : la recherche n'en donne qu'une ; les suivantes arrivent par mise à jour.
        "photos": item.photo_urls[:3] or ([item.photo_url] if item.photo_url else []),
        # Disponibilité (« status » désigne déjà l'état de l'article : très bon état…).
        "availability": "available", "availability_ts": None, "availability_after_s": None,
    }


class UiNotifier:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    async def send(self, item, deal, search) -> None:
        self.engine.push_alert(alert_payload(item, deal, search))

    async def photos(self, item_id: int, urls: list[str], handle) -> None:
        self.engine.update_alert(item_id, {"photos": urls})

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
        "max_price": _float_or_none(body.get("max_price")),
        "only_deals": bool(body.get("only_deals")),
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

    def _load(self) -> dict:
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        with contextlib.suppress(FileNotFoundError, ValueError):
            saved = json.loads(self.path.read_text("utf-8"))
            for key, value in saved.items():
                if isinstance(value, dict) and isinstance(settings.get(key), dict):
                    settings[key].update(value)
                else:
                    settings[key] = value
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
        if not cfg.searches:
            return
        self.notifiers = build_notifiers(cfg)
        self.monitor = Monitor(cfg, self.store, [UiNotifier(self), *self.notifiers])
        self.task = asyncio.create_task(self._run(self.monitor))

    async def _run(self, monitor: Monitor) -> None:
        try:
            await monitor.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Le moniteur s'est arrêté")
            self.crash = f"{type(e).__name__}: {e}"

    async def stop(self, persist: bool = False) -> None:
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

    def push_alert(self, payload: dict) -> None:
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
