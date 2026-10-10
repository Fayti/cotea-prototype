"""GhostLoc pour ordinateur : serveur local + interface web (PC et iPhone sur le même Wi-Fi)."""

import argparse
import asyncio
import logging
import platform
import secrets
import socket
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from packaging.version import Version

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
    update: dict = {}

    async def check_update() -> None:
        # Le site publie version.json : on prévient si une version plus récente existe.
        site = cfg.get("siteUrl", "")
        if not site:
            return
        try:
            data = (await asyncio.to_thread(requests.get, f"{site}/version.json", timeout=10)).json()
            if Version(str(data["version"])) > Version(__version__):
                update.update(
                    version=str(data["version"]),
                    notes=str(data.get("notes", ""))[:200],
                    url=f"{site}/installation.html",
                )
        except Exception as e:
            log.info("vérification de mise à jour impossible : %s", e)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        watcher = asyncio.create_task(controller.watch_device())
        asyncio.create_task(check_update())
        await controller.refresh_entitlement()
        yield
        watcher.cancel()
        await controller.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Rebinding DNS : on n'accepte que les noms d'hôte attendus.
        host_header = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        if host_header not in LOOPBACK and host_header != lan_ip():
            return JSONResponse({"error": "Hôte non autorisé."}, status_code=403)
        # Un site web tiers ne peut pas ajouter cet en-tête sans autorisation CORS (que l'on ne donne pas).
        if request.method != "GET" and request.headers.get("x-ghostloc") != "1":
            return JSONResponse({"error": "Requête refusée."}, status_code=403)
        # Depuis un autre appareil (iPhone sur le Wi-Fi), il faut la clé affichée par l'app.
        client = request.client.host if request.client else ""
        if client not in LOOPBACK:
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
            "update": update or None,
        }
        return snap

    @app.get("/api/diagnostic")
    async def diagnostic():
        """Résumé à copier pour le support : versions, état, fin du journal (sans jeton ni mot de passe)."""
        lines: list[str] = []
        log_file = data_dir() / "ghostloc.log"
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        snap = controller.snapshot()
        acc, dev = snap["account"], snap["device"]
        text = "\n".join([
            f"GhostLoc {__version__} · {platform.system()} {platform.release()} · Python {platform.python_version()}",
            f"Compte : {acc['email'] or 'non connecté'} · formule : {(acc['entitlement'] or {}).get('plan')}",
            f"iPhone : {dev['status']} · {dev['name']} · iOS {dev['ios']} · {dev['connection']}",
            f"Message : {dev['message']}",
            "---",
            *lines,
        ])
        return JSONResponse({"text": text})

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


def selftest() -> int:
    """Importe tout ce que le chemin iPhone réel charge à la demande (utile après PyInstaller)."""
    import importlib

    modules = [
        "pymobiledevice3.usbmux",
        "pymobiledevice3.lockdown",
        "pymobiledevice3.exceptions",
        "pymobiledevice3.remote.userspace_tunnel",
        "pymobiledevice3.services.mobile_image_mounter",
        "pymobiledevice3.services.dvt.instruments.dvt_provider",
        "pymobiledevice3.services.dvt.instruments.location_simulation",
        "pymobiledevice3.services.simulate_location",
        "pymobiledevice3.services.amfi",
        "pymobiledevice3.services.cryptexd",
    ]
    for name in modules:
        importlib.import_module(name)
    assert (ui_dir() / "index.html").exists(), "interface absente"
    cfg = load_config()
    print(f"GhostLoc {__version__} selftest OK — {len(modules)} modules, serveur : {cfg['supabaseUrl'] or '(non configuré)'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="ghostloc")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="iPhone simulé, pour tester sans appareil")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--local-only", action="store_true", help="refuser les connexions depuis le Wi-Fi")
    parser.add_argument("--selftest", action="store_true", help="vérifie que l'exécutable embarque tout le nécessaire")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(selftest())

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
