"""Chargement de la configuration (TOML pour la ligne de commande, JSON pour l'interface)."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

# Paramètres scalaires transmis tels quels à l'API.
_SCALAR_PARAMS = {"search_text", "price_from", "price_to"}


def _api_key(key: str) -> str | None:
    """Nom de paramètre du site -> nom attendu par l'API svc-catalogue (septembre 2026).

    L'API ignore en silence les anciens noms (`brand_ids`, `catalog_ids`…) : les filtres
    de catégorie, marque, taille, état, couleur, matière passent par `attribute_ids[...]`.
    """
    if key in _SCALAR_PARAMS:
        return key
    if key == "catalog":
        return "attribute_ids[catalog]"
    if key.endswith("_ids") and key not in ("search_ids",):
        return f"attribute_ids[{key[:-4]}]"
    if key.startswith("attribute_ids[") and key.endswith("]"):
        return key
    return None


def parse_vinted_url(url: str) -> tuple[str, dict[str, str]]:
    """Transforme une URL de recherche Vinted copiée du navigateur en (domaine, params API)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host.startswith("vinted."):
        raise ValueError(f"URL non Vinted : {url}")
    grouped: dict[str, list[str]] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        api_key = _api_key(key.removesuffix("[]"))
        if api_key:
            grouped.setdefault(api_key, []).extend(v for v in value.split(",") if v)
    return host, {k: ",".join(v) for k, v in grouped.items() if v}


@dataclass
class Search:
    name: str
    domain: str = "vinted.fr"
    params: dict[str, str] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    include_any: list[str] = field(default_factory=list)
    max_price: float | None = None
    min_price: float | None = None
    exclude_sellers: list[str] = field(default_factory=list)
    only_deals: bool = False
    weight: int = 1
    key: str = ""  # identifiant stable pour la base (filigrane, prix) ; le nom par défaut

    def __post_init__(self):
        self.key = self.key or self.name

    def api_params(self, per_page: int) -> dict[str, str]:
        p = dict(self.params)
        p["order"] = "newest_first"
        p["per_page"] = str(per_page)
        p["page"] = "1"
        return p


@dataclass
class Config:
    searches: list[Search]
    request_gap: float = 1.0
    max_inflight: int = 3
    use_direct: bool = True
    stats_every: float = 60.0
    max_age: float = 3600.0
    per_page: int = 20
    warmup_per_page: int = 96
    db_path: str = "vinted_bot.sqlite3"
    proxies: list[str] = field(default_factory=list)
    impersonate: str = "chrome"
    deal_window: int = 200
    deal_min_samples: int = 15
    deal_threshold: float = 0.30
    telegram_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook: str = ""
    console: bool = True


def _build_search(raw: dict) -> Search:
    if "name" not in raw:
        raise ValueError("Chaque [[search]] doit avoir un champ 'name'")
    domain = raw.get("domain", "vinted.fr")
    params: dict[str, str] = {}
    if raw.get("url"):
        domain, params = parse_vinted_url(raw["url"])
    if raw.get("search_text"):
        params["search_text"] = raw["search_text"]
    for k, v in (raw.get("params") or {}).items():
        params[k] = ",".join(map(str, v)) if isinstance(v, list) else str(v)
    max_price = raw.get("max_price")
    if max_price is not None and "price_to" not in params:
        # Filtrer côté serveur réduit la réponse et évite de gaspiller les places de la page.
        params["price_to"] = str(max_price)
    return Search(
        name=raw["name"],
        domain=domain,
        params=params,
        exclude=[w.lower() for w in raw.get("exclude", [])],
        include_any=[w.lower() for w in raw.get("include_any", [])],
        max_price=max_price,
        min_price=raw.get("min_price"),
        exclude_sellers=[s.lower() for s in raw.get("exclude_sellers", [])],
        only_deals=bool(raw.get("only_deals", False)),
        weight=max(1, int(raw.get("weight", 1))),
        key=str(raw.get("id") or ""),
    )


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = json.load(f) if path.endswith(".json") else tomllib.load(f)
    return config_from_dict(raw)


def config_from_dict(raw: dict, require_searches: bool = True) -> Config:
    general = raw.get("general", {})
    deals = raw.get("deals", {})
    telegram = raw.get("telegram", {})
    discord = raw.get("discord", {})
    searches = [_build_search(s) for s in raw.get("search", []) if s.get("enabled", True)]
    if require_searches and not searches:
        raise ValueError("Aucune recherche [[search]] définie dans la configuration")
    keys = [s.key for s in searches]
    if len(keys) != len(set(keys)):
        raise ValueError("Les noms de recherche doivent être uniques")
    return Config(
        searches=searches,
        request_gap=float(general.get("request_gap", 1.0)),
        max_inflight=int(general.get("max_inflight", 3)),
        use_direct=bool(general.get("use_direct", True)),
        stats_every=float(general.get("stats_every", 60)),
        max_age=float(general.get("max_age_minutes", 60)) * 60,
        per_page=int(general.get("per_page", 20)),
        warmup_per_page=int(general.get("warmup_per_page", 96)),
        db_path=general.get("db_path", "vinted_bot.sqlite3"),
        proxies=list(general.get("proxies", [])),
        impersonate=general.get("impersonate", "chrome"),
        deal_window=int(deals.get("window", 200)),
        deal_min_samples=int(deals.get("min_samples", 15)),
        deal_threshold=float(deals.get("threshold", 0.30)),
        telegram_token=os.environ.get("TELEGRAM_TOKEN") or telegram.get("token", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or str(telegram.get("chat_id", "")),
        discord_webhook=os.environ.get("DISCORD_WEBHOOK") or discord.get("webhook_url", ""),
        console=bool(general.get("console", True)),
    )
