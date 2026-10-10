"""GhostLoc pour ordinateur : serveur local + interface web (PC et iPhone sur le même Wi-Fi)."""

import argparse
import asyncio
import logging
import secrets
import socket
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from ghostloc_desktop import __version__
from ghostloc_desktop.account import Account
from ghostloc_desktop.config import data_dir, load_config, ui_dir
from ghostloc_desktop.controller import Controller, UserError
from ghostloc_desktop.device import DemoLink, IPhoneLink

log = logging.getLogger("ghostloc")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # aucune donnée envoyée, sert à choisir l'interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def build_app(controller: Controller, cfg: dict, port: int, key: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        watcher = asyncio.create_task(controller.watch_device())
        await controller.refresh_entitlement()
        yield
        watcher.cancel()
        await controller.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def lan_guard(request: Request, call_next):
        # Depuis un autre appareil (iPhone sur le Wi-Fi), il faut la clé affichée par l'app.
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            supplied = request.query_params.get("k") or request.cookies.get("glk")
            if not supplied or not secrets.compare_digest(supplied, key):
                return JSONResponse({"error": "Scanne le QR code affiché sur ton ordinateur."}, status_code=403)
        response = await call_next(request)
        if request.query_params.get("k") == key:
            response.set_cookie("glk", key, max_age=3600 * 24 * 365, httponly=True, samesite="strict")
        return response

    @app.get("/")
    async def index():
        return FileResponse(ui_dir() / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse({
            "name": "GhostLoc", "short_name": "GhostLoc", "start_url": "/", "display": "standalone",
            "background_color": "#0b0b0d", "theme_color": "#fffc00",
        })

    @app.get("/api/state")
    async def state():
        snap = controller.snapshot()
        snap["app"] = {
            "version": __version__, "siteUrl": cfg.get("siteUrl", ""),
            "lanUrl": f"http://{lan_ip()}:{port}/?k={key}",
        }
        return snap

    async def run(make_action):
        try:
            await make_action()
        except UserError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return controller.snapshot()

    def point(body: dict) -> tuple[float, float]:
        try:
            lat, lng = float(body["lat"]), float(body["lng"])
        except (KeyError, TypeError, ValueError):
            raise UserError("Coordonnées invalides.") from None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise UserError("Coordonnées invalides.")
        return lat, lng

    def label(body: dict, default: str) -> str:
        return str(body.get("name") or default).strip()[:80] or default

    @app.post("/api/login")
    async def login(body: dict):
        return await run(lambda: controller.login(str(body.get("email", "")).strip(), str(body.get("password", ""))))

    @app.post("/api/logout")
    async def logout():
        return await run(controller.logout)

    @app.post("/api/refresh")
    async def refresh():
        return await run(controller.refresh_entitlement)

    @app.post("/api/connect")
    async def connect():
        return await run(controller.connect)

    @app.post("/api/devmode")
    async def devmode():
        return await run(controller.reveal_developer_mode)

    @app.post("/api/teleport")
    async def teleport(body: dict):
        return await run(lambda: controller.teleport(*point(body), label(body, "Point choisi")))

    @app.post("/api/route")
    async def route(body: dict):
        speed = body.get("speed")
        return await run(lambda: controller.start_route(
            *point(body), label(body, "Destination"), str(body.get("mode", "voiture")),
            float(speed) if isinstance(speed, (int, float)) else None,
        ))

    @app.post("/api/route/stop")
    async def route_stop():
        return await run(controller.stop_route)

    @app.post("/api/home")
    async def home():
        return await run(controller.go_home)

    return app


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="ghostloc")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="iPhone simulé, pour tester sans appareil")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--local-only", action="store_true", help="refuser les connexions depuis le Wi-Fi")
    args = parser.parse_args()

    folder = data_dir()
    handler = RotatingFileHandler(folder / "ghostloc.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler(sys.stdout)],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    url = f"http://127.0.0.1:{args.port}/"
    if port_in_use(args.port):
        # Déjà lancé : on ouvre simplement l'interface.
        webbrowser.open(url)
        return

    key_file = folder / "lan.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(18), encoding="utf-8")
    key = key_file.read_text(encoding="utf-8").strip()

    cfg = load_config()
    account = Account(cfg["supabaseUrl"], cfg["supabaseAnonKey"], folder / "session.json")
    controller = Controller(account, DemoLink() if args.demo else IPhoneLink())
    app = build_app(controller, cfg, args.port, key)

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"GhostLoc {__version__} — interface : {url}")
    print(f"Depuis ton iPhone (même Wi-Fi) : http://{lan_ip()}:{args.port}/?k={key}")
    uvicorn.run(app, host="127.0.0.1" if args.local_only else "0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
