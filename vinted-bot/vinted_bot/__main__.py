"""Point d'entrée : python -m vinted_bot {run,check,test-notify} -c config.toml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import filters
from .config import Config, load_config
from .models import Deal, Item
from .monitor import Monitor
from .notifier import build_notifiers, format_text, sample_alert
from .store import Store


async def cmd_run(cfg: Config) -> None:
    notifiers = build_notifiers(cfg)
    store = Store(cfg.db_path)
    try:
        await Monitor(cfg, store, notifiers).run()
    finally:
        for n in notifiers:
            await n.close()
        store.close()


async def cmd_check(cfg: Config) -> None:
    """Une requête par recherche, affichage des résultats bruts filtrés, sans rien enregistrer."""
    monitor = Monitor(cfg, store=None, notifiers=[])
    try:
        for search in cfg.searches:
            raw = await monitor.client(search.domain).search(search.api_params(cfg.per_page))
            items = [Item.from_api(r, search.domain) for r in raw]
            kept = [i for i in items if filters.accept(i, search)[0]]
            print(f"\n=== {search.name} ({search.domain}) : {len(items)} reçues, {len(kept)} gardées ===")
            for item in kept[:5]:
                print(format_text(item, Deal(None, None, False), search), end="\n\n")
    finally:
        for c in monitor.clients.values():
            await c.close()


async def cmd_test_notify(cfg: Config) -> None:
    item, deal = sample_alert()
    for n in build_notifiers(cfg):
        try:
            await n.send(item, deal, cfg.searches[0])
            print(f"OK  {type(n).__name__}")
        except Exception as e:
            print(f"ERR {type(n).__name__} : {e}")
        await n.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Console Windows : un emoji dans un titre d'annonce ne doit jamais faire planter le bot.
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(prog="vinted_bot", description="Alertes Vinted en quasi temps réel")
    parser.add_argument("command", nargs="?", default="ui",
                        choices=["ui", "run", "check", "test-notify", "selftest"],
                        help="ui (défaut) = interface dans le navigateur")
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command in ("ui", "selftest"):
        from pathlib import Path

        from . import app

        try:
            if args.command == "selftest":
                asyncio.run(app.selftest())
            else:
                data_dir = Path(args.data_dir) if args.data_dir else app.default_data_dir()
                asyncio.run(app.run_ui(data_dir, args.port, open_browser=not args.no_browser))
        except KeyboardInterrupt:
            pass
        return 0

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"Configuration invalide : {e}", file=sys.stderr)
        return 2
    command = {"run": cmd_run, "check": cmd_check, "test-notify": cmd_test_notify}[args.command]
    try:
        asyncio.run(command(cfg))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Erreur : {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
