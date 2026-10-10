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
from .notifier import ConsoleNotifier, DiscordNotifier, TelegramNotifier, format_text
from .store import Store


def build_notifiers(cfg: Config) -> list:
    notifiers = []
    if cfg.console:
        notifiers.append(ConsoleNotifier())
    if cfg.telegram_token and cfg.telegram_chat_id:
        notifiers.append(TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id))
    if cfg.discord_webhook:
        notifiers.append(DiscordNotifier(cfg.discord_webhook))
    return notifiers


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
    item = Item(
        id=1, title="Test — Nike Air Max 90", price=25.0, currency="EUR", total_price=27.95,
        brand="Nike", size="42", status="Très bon état", url="https://www.vinted.fr/",
        photo_url="", seller="vinted_bot", created_ts=None, favourites=0,
    )
    deal = Deal(median=45.0, discount=1 - 25 / 45, is_deal=True)
    notifiers = build_notifiers(cfg)
    for n in notifiers:
        try:
            await n.send(item, deal, cfg.searches[0])
            print(f"OK  {type(n).__name__}")
        except Exception as e:
            print(f"ERR {type(n).__name__} : {e}")
        await n.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinted_bot", description="Alertes Vinted en quasi temps réel")
    parser.add_argument("command", choices=["run", "check", "test-notify"])
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
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
