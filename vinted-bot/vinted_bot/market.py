"""Veille du marché : ce qui se vend, à quel prix, à quelle vitesse, et où sont les pépites.

Vinted ne publie aucun prix de vente. On le reconstitue : chaque segment surveillé (une
marque, un type d'article) est relu toutes les quelques minutes, 96 annonces à la fois, des
plus récentes aux plus anciennes. Chaque annonce est suivie : prix, favoris (« likes »), vues.
Quand elle disparaît alors que des annonces plus anciennes sont toujours là, elle est partie :
sa page dit alors si elle est vendue (au dernier prix vu), réservée, supprimée ou encore en
vente. Mesuré sur le vrai Vinted, beaucoup de disparitions sont des suppressions : seules les
ventes confirmées comptent. Une annonce qui sort simplement de la fenêtre (trop de nouveautés
derrière elle) n'est pas comptée.

Une « pépite » est une annonce partie en moins d'une heure à un prix bien inférieur au prix
de vente habituel de son segment : quelqu'un a acheté sous le marché. Leur somme par jour
mesure l'argent qu'un acheteur rapide peut gagner sur ce segment.

Coût en requêtes : un segment toutes les POLL_EVERY secondes, partagé avec la surveillance
(même rythme, mêmes protections anti-blocage).
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import statistics
import time
import unicodedata
from collections import Counter, defaultdict

from .config import parse_vinted_url
from .models import Item

log = logging.getLogger(__name__)

POLL_EVERY = 180.0      # secondes entre deux relectures d'un même segment
PER_PAGE = 96
NEIGHBOURS = 20         # annonces plus anciennes retenues comme repères (une fenêtre ne perd
                        # que ses dernières annonces : une absence au milieu est un départ)
MISSING_POLLS = 2       # absences consécutives avant de la dire partie
FAST = 3600.0           # « partie vite » : en moins d'une heure
PEPITE_RATIO = 0.6      # prix ≤ 60 % du prix de vente habituel
PEPITE_FLOOR = 0.25     # … mais ≥ 25 % : en dessous, c'est un accessoire, un emballage ou un faux
# Emballages et à-côtés vendus sous le nom d'une grande marque (« boîte Loro Piana », « sac
# papier Dior »…) : écartés de la veille, ils passeraient pour des pépites à 5 €.
JUNK = re.compile(
    r"\b(boite|box vide|empty box|emballage|packaging|sac (en )?papier|sacs (en )?papier|paper bag|shopping bag|"
    r"sac shopping|sac cadeau|gift bag|dust ?bag|pochon|pochette de protection|housse|cintre|ruban|ribbon|"
    r"certificat|certificate|facture|ticket|etiquette|carte cadeau|gift card|catalogue|magazine|flyer|"
    r"autocollant|sticker|notice|mode d.emploi|boucle seule|bouton(s)? seul)", re.I)


def is_junk(title: str) -> bool:
    return bool(JUNK.search(_norm(title)))
MIN_SOLD = 8            # ventes minimum pour estimer un prix de vente habituel
KEEP_DAYS = 14
SOLD_STATES = ("sold", "reserved")   # vente confirmée par la page de l'annonce
ALERT_MIN_GAIN = 15.0   # pépite signalée tout de suite si le gain estimé atteint ce montant
MEDIAN_TTL = 600.0      # prix de vente habituel recalculé toutes les 10 min par segment
RECENT_IDS = 150_000     # ~10 min de numéros (Vinted en attribue ~250/s) : seuil de « nouvelle »
RADAR_HOURS = 6.0       # le radar lit assez de pages pour couvrir ~6 h d'annonces
RADAR_START_PAGES = 5
RADAR_MAX_PAGES = 10    # l'API refuse au-delà de la page 10 (mesuré : page 11 -> HTTP 400)
# Contrôle au hasard : une annonce du radar mise en ligne il y a SAMPLE_AGE est tirée au sort
# toutes les SAMPLE_EVERY secondes, et sa page dit si elle s'est vendue. Mesuré sur le vrai
# Vinted : les disparitions des résultats sont surtout des suppressions ou des annonces encore
# en vente ; ce sondage donne le vrai taux de vente, sans faux positif.
SAMPLE_AGE = 12 * 3600.0
SAMPLE_EVERY = 20.0


def radar_bands(min_price: float) -> list[dict]:
    """Le radar découpe ses prix en 3 tranches, chacune lue sur 10 pages au plus : la fenêtre
    d'observation de chaque tranche dure ainsi bien plus longtemps qu'une seule liste."""
    m = float(min_price)
    cuts = [(m, round(m * 1.5)), (round(m * 1.5), round(m * 2.5)), (round(m * 2.5), None)]
    bands = []
    for i, (low, high) in enumerate(cuts, 1):
        name = f"Radar {low:.0f}–{high:.0f} €" if high else f"Radar ≥ {low:.0f} €"
        bands.append({"id": f"radar-{i}", "name": name, "query": "", "price_from": low,
                      "price_to": (high - 0.01) if high else None, "radar": True})
    return bands


def buy_cost(price: float) -> float:
    """Prix payé sur Vinted : article + protection acheteurs (~5 % + 0,70 €) + envoi (~5 €)."""
    return price * 1.05 + 0.70 + 5.0


def segment_params(seg: dict) -> tuple[str, dict[str, str]]:
    """Segment de veille -> (domaine, paramètres de l'API). Un lien Vinted ou des mots-clés."""
    query = (seg.get("query") or "").strip()
    if query.startswith("http"):
        domain, params = parse_vinted_url(query)
    else:
        domain, params = "vinted.fr", ({"search_text": query} if query else {})
    if seg.get("price_from"):
        params["price_from"] = str(int(seg["price_from"]))
    if seg.get("price_to"):
        params["price_to"] = f"{seg['price_to']:.2f}"
    return domain, params


# Segments proposés : revente active, contrefaçon faible ou modérée. Les marques très
# contrefaites (Moncler, Stone Island, CP Company, Canada Goose, Nuptse…) sont exclues : des
# faux vendus vite et pas cher passeraient pour des pépites.
DEFAULT_SEGMENTS = [
    ("Herno doudoune", "herno doudoune"), ("Pyrenex doudoune", "pyrenex doudoune"),
    ("Arc'teryx veste", "arcteryx veste"), ("Patagonia polaire", "patagonia polaire"),
    ("Patagonia doudoune", "patagonia doudoune"), ("Barbour veste", "barbour veste"),
    ("Carhartt Detroit", "carhartt detroit"), ("Salomon XT-6", "salomon xt-6"),
    ("New Balance 990", "new balance 990"), ("New Balance 2002R", "new balance 2002r"),
    ("Asics Gel-Kayano 14", "asics kayano 14"), ("Loro Piana", "loro piana"),
    ("Brunello Cucinelli", "brunello cucinelli"), ("Acne Studios", "acne studios"),
    ("Our Legacy", "our legacy"), ("Maison Margiela", "maison margiela"),
    ("A.P.C.", "apc"), ("Isabel Marant", "isabel marant"), ("Sézane", "sezane"),
    ("Rouje", "rouje"), ("Levi's 501 vintage", "levis 501 vintage"),
    ("Appareil photo argentique", "appareil photo argentique"), ("Lego lot", "lego lot"),
    ("Game Boy", "game boy"),
]


class MarketWatch:
    def __init__(self, store, client_of, segments: list[dict], poll_every: float = POLL_EVERY,
                 memory: dict | None = None, on_pepite=None, on_gone=None):
        self.store = store
        self.on_pepite = on_pepite          # rappel (annonce, segment, prix habituel, gain)
        self.on_gone = on_gone              # rappel (annonce, lien, domaine) : départ à vérifier
        self._medians: dict[str, tuple[float | None, float]] = {}
        self.client_of = client_of          # domaine -> VintedClient (partagé avec la surveillance)
        self.segments = segments
        self.poll_every = poll_every
        # Mémoire gardée par l'appelant : un redémarrage du moteur (réglage modifié) ne fait
        # pas tout reprendre de zéro.
        memory = memory if memory is not None else {}
        self._prev: dict[str, list[int]] = memory.setdefault("prev", {})       # ids du passage précédent
        self._missing: dict[int, tuple[int, float, str]] = memory.setdefault("missing", {})  # absences
        self._neighbours: dict[int, tuple[int, ...]] = memory.setdefault("neighbours", {})
        self.last_poll: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.rate: dict[str, float] = memory.setdefault("rate", {})    # nouvelles annonces / heure
        self.pages: dict[str, int] = {}
        self._radar = {s["id"] for s in segments if s.get("radar")}

    def window_hours(self, seg_id: str) -> float | None:
        """Durée pendant laquelle une annonce reste visible dans la fenêtre lue."""
        rate, pages = self.rate.get(seg_id), self.pages.get(seg_id)
        return round(pages * PER_PAGE / rate, 1) if rate and pages else None

    def pages_for(self, seg: dict) -> int:
        """Radar : assez de pages pour couvrir RADAR_HOURS d'annonces (une vente plus tardive
        sortirait de la fenêtre avant d'être vue). Segment ordinaire : une page."""
        if not seg.get("radar"):
            return 1
        rate = self.rate.get(seg["id"])
        if rate is None:
            return RADAR_START_PAGES
        return max(2, min(RADAR_MAX_PAGES, math.ceil(rate * RADAR_HOURS / PER_PAGE) + 1))

    def median_sold(self, seg_id: str, now: float) -> float | None:
        """Prix de vente habituel du segment (médiane des ventes des 7 derniers jours)."""
        median, ts = self._medians.get(seg_id, (None, 0.0))
        # Pas encore assez de ventes : on retente vite plutôt que d'attendre 10 minutes.
        if now - ts > (MEDIAN_TTL if median else 60.0):
            prices = self.store.market_sold_prices(seg_id, now - 7 * 86400)
            median = statistics.median(prices) if len(prices) >= MIN_SOLD else None
            self._medians[seg_id] = (median, now)
        return median

    async def run(self) -> None:
        if not self.segments:
            return
        log.info("Veille du marché : %d segment(s), relus toutes les %d s", len(self.segments), self.poll_every)
        await asyncio.gather(self._poll_loop(), self._sample_loop())

    async def _poll_loop(self) -> None:
        gap = self.poll_every / len(self.segments)   # étalées : pas de rafale
        while True:
            for seg in self.segments:
                started = time.monotonic()
                await self.poll(seg)
                await asyncio.sleep(max(1.0, gap - (time.monotonic() - started)))

    async def _sample_loop(self, age: float = SAMPLE_AGE, every: float = SAMPLE_EVERY) -> None:
        """Radar : contrôle au hasard d'annonces assez anciennes (vendues ou non ?)."""
        if not self._radar or not self.on_gone:
            return
        domain = segment_params(next(s for s in self.segments if s.get("radar")))[0]
        while True:
            await asyncio.sleep(every)
            for iid, url in self.store.market_sample("radar", time.time() - age):
                self.on_gone(iid, url, domain)

    async def poll(self, seg: dict) -> None:
        raw: list[dict] = []
        try:
            domain, params = segment_params(seg)
            pages = self.pages_for(seg)
            for page in range(1, pages + 1):
                batch = await self.client_of(domain).search(
                    {**params, "order": "newest_first", "per_page": str(PER_PAGE), "page": str(page)})
                raw.extend(r for r in batch if isinstance(r, dict))
                if len(batch) < PER_PAGE:
                    break  # fin des résultats
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Lecture incomplète : rien n'est conclu (les annonces non relues passeraient pour parties).
            self.errors[seg["id"]] = str(e)[:200]
            log.debug("Veille %s : %s", seg["name"], e)
            await asyncio.sleep(30)
            return
        self.errors.pop(seg["id"], None)
        self.pages[seg["id"]] = pages
        # Entre deux pages, les nouveautés décalent les annonces : doublons possibles, retirés.
        seen, unique = set(), []
        for r in raw:
            if r.get("id") not in seen:
                seen.add(r.get("id"))
                unique.append(r)
        self.process(seg["id"], unique, time.time(), domain)

    def process(self, seg_id: str, raw_items: list[dict], now: float, domain: str = "vinted.fr") -> None:
        items = []
        for r in raw_items:
            try:
                item = Item.from_api(r, domain)
            except (KeyError, TypeError, ValueError):
                continue
            if not item.promoted and not is_junk(item.title):
                items.append(item)
        ids = [i.id for i in items]
        prev = self._prev.get(seg_id)
        # Âge connu seulement pour les annonces apparues entre deux passages, avec un numéro
        # récent. Au premier passage (ou après un arrêt), les annonces présentes ont un âge
        # inconnu : suivies, mais exclues des délais de vente. Idem pour une vieille annonce qui
        # refait surface, ou qui devient visible quand la fenêtre lue s'allonge (plus de pages).
        fresh = set()
        if prev:
            floor, before = max(prev) - RECENT_IDS, set(prev)
            fresh = {iid for iid in ids if iid not in before and iid >= floor}
        self.store.market_upsert(seg_id, items, now, fresh)
        if prev is not None and seg_id in self.last_poll:
            hours = max(1e-6, (now - self.last_poll[seg_id]) / 3600)
            old = self.rate.get(seg_id)
            self.rate[seg_id] = len(fresh) / hours if old is None else 0.7 * old + 0.3 * len(fresh) / hours
        if fresh and self.on_pepite and seg_id not in self._radar:
            median = self.median_sold(seg_id, now)
            for item in items:
                if (item.id in fresh and median and item.price is not None
                        and PEPITE_FLOOR * median <= item.price <= PEPITE_RATIO * median):
                    gain = round(median - buy_cost(item.price), 2)
                    if gain >= ALERT_MIN_GAIN:
                        self.on_pepite(item, seg_id, median, gain)

        present = set(ids)
        if prev is not None:
            gone, out = [], []
            # Suivies : celles vues au passage précédent, plus celles en attente de confirmation.
            for iid in set(prev) | {i for i, (_, _, s) in self._missing.items() if s == seg_id}:
                if iid in present:
                    self._missing.pop(iid, None)
                    continue
                older = self._neighbours.get(iid, ())
                if not any(o in present for o in older):
                    out.append(iid)                 # sortie de la fenêtre : on ne peut pas savoir
                    self._missing.pop(iid, None)
                    self._neighbours.pop(iid, None)
                    continue
                count, since, _ = self._missing.get(iid, (0, now, seg_id))
                count += 1
                if count >= MISSING_POLLS:
                    gone.append((iid, since))
                    self._missing.pop(iid, None)
                    self._neighbours.pop(iid, None)
                else:
                    self._missing[iid] = (count, since, seg_id)
            self.store.market_mark(gone, out)
            if gone and self.on_gone:
                # Une disparition n'est pas toujours une vente (mesuré : souvent une suppression).
                # La page de l'annonce tranche : vendue, réservée, supprimée ou encore en vente.
                for iid, url in self.store.market_urls([iid for iid, _ in gone]):
                    self.on_gone(iid, url, domain)
        # Repères de chaque annonce présente : les annonces plus anciennes juste après elle.
        for i, iid in enumerate(ids):
            self._neighbours[iid] = tuple(ids[i + 1:i + 1 + NEIGHBOURS])
        self._prev[seg_id] = ids
        self.last_poll[seg_id] = now


# --- analyse -------------------------------------------------------------------------------

def _median(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def _row_hours(r: dict) -> float:
    return max(0.0, (r["last_seen"] - r["first_seen"]) / 3600)


def report(rows: list[dict], segments: list[dict], now: float | None = None) -> dict:
    """Tableau de veille : classement des segments, pépites en ligne, ventes rapides et chères."""
    now = now or time.time()
    names = {s["id"]: s["name"] for s in segments}
    by_seg: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["segment"] in names:
            by_seg[r["segment"]].append(r)

    seg_rows, sold_median = [], {}
    for seg in segments:
        rs = by_seg.get(seg["id"], [])
        fresh = [r for r in rs if r["fresh"]]
        sold = [r for r in fresh if r["state"] in SOLD_STATES and r["gone_ts"]]
        for r in sold:
            r["sell_s"] = max(0.0, r["gone_ts"] - r["first_seen"])
        median_sold = _median([r["price"] for r in sold]) if len(sold) >= MIN_SOLD else None
        sold_median[seg["id"]] = median_sold
        first = min((r["first_seen"] for r in fresh), default=now)
        days = max(1.0, (now - first) / 86400)   # pas d'extrapolation le premier jour
        mature = [r for r in fresh if now - r["first_seen"] >= 86400 and r["state"] in ("live", *SOLD_STATES)]
        sold_24h = [r for r in mature if r["state"] in SOLD_STATES and r.get("sell_s", 1e12) <= 86400]
        pepites = []
        if median_sold:
            for r in sold:
                if r["sell_s"] <= FAST and PEPITE_FLOOR * median_sold <= r["price"] <= PEPITE_RATIO * median_sold:
                    gain = median_sold - buy_cost(r["price"])
                    if gain > 0:
                        pepites.append(gain)
        liked = [r for r in fresh if _row_hours(r) >= 0.5]
        row = {
            "id": seg["id"], "name": seg["name"],
            "listings": len(rs), "new_per_day": round(len(fresh) / days, 1),
            "pending": sum(1 for r in rs if r["state"] == "gone"),
            "deleted": sum(1 for r in rs if r["state"] == "deleted"),
            "median_list": _median([r["price"] for r in rs]),
            "sold": len(sold), "median_sold": median_sold,
            "median_sell_s": _median([r["sell_s"] for r in sold]),
            "sold_24h_rate": round(len(sold_24h) / len(mature), 3) if len(mature) >= 5 else None,
            "pepites_per_day": round(len(pepites) / days, 2),
            "money_per_day": round(sum(pepites) / days, 2),
            "likes_per_h": _median([(r["likes"] - r["likes0"]) / _row_hours(r) for r in liked]),
        }
        if len(sold) < MIN_SOLD:
            row["verdict"] = f"Données en cours ({len(sold)} vente(s) vues, {MIN_SOLD} nécessaires)"
        elif row["money_per_day"] >= 50 and row["pepites_per_day"] >= 1:
            row["verdict"] = "🥇 De l'or : des pépites partent chaque jour, sois le plus rapide"
        elif row["money_per_day"] >= 15:
            row["verdict"] = "🥈 Rentable : des affaires régulières"
        elif row["sold_24h_rate"] is not None and row["sold_24h_rate"] >= 0.3:
            row["verdict"] = "🔄 Ça se vend bien, mais rarement sous le prix du marché"
        else:
            row["verdict"] = "🐢 Peu d'affaires"
        seg_rows.append(row)
    seg_rows.sort(key=lambda r: (-(r["money_per_day"] or 0), -(r["sold_24h_rate"] or 0)))

    live_pepites, fast_dear, most_liked = [], [], []
    for r in rows:
        if r["segment"] not in names:
            continue
        median_sold = sold_median.get(r["segment"])
        card = {k: r[k] for k in ("item_id", "title", "price", "currency", "url", "photo", "likes", "views",
                                  "brand", "size", "condition")}
        card["segment"] = names[r["segment"]]
        card["median_sold"] = median_sold
        hours = _row_hours(r)
        card["likes_per_h"] = round((r["likes"] - r["likes0"]) / hours, 2) if hours >= 0.5 else None
        if r["state"] == "live" and now - r["last_seen"] <= 900:
            if median_sold and PEPITE_FLOOR * median_sold <= r["price"] <= 0.7 * median_sold:
                card["gain"] = round(median_sold - buy_cost(r["price"]), 2)
                if card["gain"] > 0:
                    live_pepites.append(card)
            if card["likes_per_h"]:
                most_liked.append(card)
        elif r["state"] in SOLD_STATES and r["fresh"] and r["gone_ts"] and r["gone_ts"] - r["first_seen"] <= 6 * 3600:
            card["sell_s"] = round(r["gone_ts"] - r["first_seen"])
            card["gone_ts"] = r["gone_ts"]
            fast_dear.append(card)
    live_pepites.sort(key=lambda c: -c["gain"])
    fast_dear.sort(key=lambda c: -(c["price"] or 0))
    most_liked.sort(key=lambda c: -c["likes_per_h"])
    return {"segments": seg_rows, "pepites": live_pepites[:24], "fast_dear": fast_dear[:30],
            "most_liked": most_liked[:20], "generated": now}


# --- radar : tout ce qui se vend au-dessus d'un prix ----------------------------------------

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# Catégorie devinée d'après le titre (l'API de recherche ne la renvoie pas). Ordre important :
# le premier groupe qui correspond l'emporte.
CATEGORIES = [
    ("Téléphones", "iphone galaxy smartphone pixel telephone"),
    ("Ordinateurs & tablettes", "macbook ipad laptop ordinateur tablette imac"),
    ("Consoles & jeux vidéo", "ps5 ps4 playstation xbox nintendo switch console steam quest vr"),
    ("Audio", "airpods casque headphones enceinte speaker bose marshall"),
    ("Photo", "appareil camera objectif lens leica fujifilm gopro reflex argentique canon nikon"),
    ("Électroménager & beauté tech", "dyson thermomix airwrap aspirateur robot nespresso kitchenaid lisseur"),
    ("Montres", "montre watch rolex omega cartier tudor"),
    ("Bijoux", "bague collier bracelet boucles bijou bijoux ring necklace earrings wedding engagement diamant diamond"),
    ("Sacs", "sac bag pochette cabas tote speedy neverfull sacoche clutch handbag"),
    ("Baskets", "basket baskets sneaker sneakers jordan dunk yeezy"),
    ("Chaussures", "chaussure chaussures bottes boots bottines escarpins mocassins derbies sandales shoes heels talons loafers"),
    ("Doudounes & manteaux", "doudoune manteau parka puffer coat trench"),
    ("Vestes", "veste blouson jacket"),
    ("Pulls & sweats", "pull sweat cardigan gilet sweater hoodie"),
    ("Robes", "robe dress"),
    ("Pantalons & jeans", "jean jeans pantalon pants trousers"),
    ("Lunettes", "lunettes sunglasses"),
    ("Puériculture", "poussette stroller cybex yoyo bugaboo"),
    ("Vélos & trottinettes", "velo trottinette bike"),
    ("Cartes à collectionner", "pokemon yu-gi-oh cartes card cards"),
    ("Lego & jouets", "lego playmobil jouet"),
    ("Parfums", "parfum perfume"),
    ("Accessoires", "ceinture belt echarpe scarf foulard portefeuille wallet casquette"),
    ("Maison & meubles", "canape chaise fauteuil table bureau commode lampe meuble dresser desk sofa"),
]
_CATEGORY_WORDS = [(name, set(words.split())) for name, words in CATEGORIES]
# Sans mot-clé dans le titre : catégorie la plus probable pour la marque, au-dessus de 200 €.
BRAND_CATEGORY = [
    ("Sacs", ["louis vuitton", "chanel", "dior", "gucci", "prada", "hermes", "celine", "fendi", "loewe",
              "goyard", "saint laurent", "ysl", "coach", "longchamp", "michael kors", "polene", "balenciaga",
              "bottega veneta", "miu miu", "chloe", "marc jacobs"]),
    ("Montres", ["rolex", "omega", "cartier", "tudor", "breitling", "tag heuer", "longines"]),
    ("Doudounes & manteaux", ["moncler", "canada goose"]),
    ("Baskets", ["jordan", "nike", "adidas", "new balance", "yeezy"]),
    ("Électroménager & beauté tech", ["dyson"]),
    ("Consoles & jeux vidéo", ["nintendo", "playstation", "xbox", "meta quest"]),
]
_STOP = set("""de la le les des du et en a au aux un une pour avec sans sur the and for with of in taille size
t tbe tres bon bonne etat neuf neuve new nwt avec etiquette homme femme men women mens womens unisex
original originale authentique authentic 100 xs s m l xl xxl xxxl eu us fr uk it cm couleur color""".split())


def guess_category(title: str, brand: str = "") -> str:
    words = set(re.findall(r"[a-z0-9-]+", _norm(title)))
    for name, keys in _CATEGORY_WORDS:
        if words & keys:
            return name
    text = f" {_norm(brand)} {_norm(title)} "
    for name, brands in BRAND_CATEGORY:
        if any(f" {b} " in text for b in brands):
            return name
    return "Autre"


def _tokens(title: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", _norm(title)) if w not in _STOP]


def _models(sold: list[dict], top: int = 25) -> list[dict]:
    """Expressions qui reviennent dans les titres vendus (« jordan 1 », « iphone 14 pro »…)."""
    counts: Counter = Counter()
    prices: dict[str, list[float]] = defaultdict(list)
    for r in sold:
        toks = _tokens(r["title"])
        grams = set()
        for n in (2, 3):
            for i in range(len(toks) - n + 1):
                gram = toks[i:i + n]
                if any(len(t) >= 3 and not t.isdigit() for t in gram):
                    grams.add(" ".join(gram))
        for g in grams:
            counts[g] += 1
            prices[g].append(r["price"])
    kept = [(g, c) for g, c in counts.items() if c >= 3]
    # « air jordan » et « air jordan 1 » vus autant de fois : on garde la plus précise.
    kept = [(g, c) for g, c in kept if not any(c2 == c and g != g2 and g in g2 for g2, c2 in kept)]
    kept.sort(key=lambda gc: (-gc[1], gc[0]))
    return [{"model": g, "sales": c, "median_price": _median(prices[g]), "revenue": round(sum(prices[g]), 2)}
            for g, c in kept[:top]]


MIN_CHECKED = 5   # contrôles minimum pour afficher un taux de vente


def _group(rows: list[dict], key, days: float) -> list[dict]:
    """Par marque ou catégorie : ventes confirmées, taux de vente mesuré par contrôle au hasard,
    ventes estimées par jour (taux × annonces publiées par jour), prix et délai."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[key(r)].append(r)
    out = []
    for name, rs in by.items():
        sold = [r for r in rs if r["state"] in SOLD_STATES]
        checked = [r for r in rs if r["sampled"] and r["state"] in (*SOLD_STATES, "deleted", "unsold")]
        checked_sold = sum(1 for r in checked if r["state"] in SOLD_STATES)
        rate = checked_sold / len(checked) if len(checked) >= MIN_CHECKED else None
        per_day = len(rs) / days
        if not sold and not checked:
            continue
        out.append({"name": name, "sales": len(sold), "revenue": round(sum(r["price"] for r in sold), 2),
                    "median_price": _median([r["price"] for r in sold]), "listed": len(rs),
                    "listed_per_day": round(per_day, 1), "checked": len(checked), "checked_sold": checked_sold,
                    "sell_rate": round(rate, 3) if rate is not None else None,
                    "est_sales_day": round(rate * per_day, 1) if rate is not None else None,
                    "median_sell_s": _median([r["sell_s"] for r in sold if r.get("sell_s") is not None])})
    out.sort(key=lambda g: (g["est_sales_day"] is None, -(g["est_sales_day"] or 0), -g["sales"], -g["revenue"]))
    return out


def radar_report(rows: list[dict], min_price: float, now: float | None = None) -> dict:
    """Tout ce qui se vend au-dessus de min_price : marques, catégories, modèles, dernières ventes."""
    now = now or time.time()
    rows = [r for r in rows if r.get("price") is not None and r["price"] >= min_price]
    for r in rows:
        r["category"] = guess_category(r["title"], r["brand"])
        r.setdefault("sampled", 0)
        r.setdefault("checked_ts", None)
        r["sell_s"] = (max(0.0, r["gone_ts"] - r["first_seen"])
                       if r["state"] in SOLD_STATES and r["fresh"] and r["gone_ts"] else None)
    sold = [r for r in rows if r["state"] in SOLD_STATES]
    checked = [r for r in rows if r["sampled"] and r["state"] in (*SOLD_STATES, "deleted", "unsold")]
    first = min((r["first_seen"] for r in rows), default=now)
    hours = max(1.0, (now - first) / 3600)
    days = max(1.0, hours / 24)
    checked_sold = sum(1 for r in checked if r["state"] in SOLD_STATES)
    recent = sorted(sold, key=lambda r: -(r["checked_ts"] or r["gone_ts"] or 0))[:40]
    return {
        "min_price": min_price, "hours": round(hours, 1), "listings": len(rows), "sales": len(sold),
        "pending": sum(1 for r in rows if r["state"] == "gone"),
        "deleted": sum(1 for r in rows if r["state"] == "deleted"),
        "hidden": sum(1 for r in rows if r["state"] == "hidden"),
        "checked": len(checked), "checked_sold": checked_sold,
        "sell_rate": round(checked_sold / len(checked), 3) if len(checked) >= MIN_CHECKED else None,
        "sample_age_h": SAMPLE_AGE / 3600,
        "revenue": round(sum(r["price"] for r in sold), 2),
        "brands": _group(rows, lambda r: (r["brand"] or "").strip() or "Sans marque", days)[:30],
        "categories": _group(rows, lambda r: r["category"], days)[:20],
        "models": _models(sold),
        "recent": [{"title": r["title"], "brand": r["brand"], "price": r["price"], "currency": r["currency"],
                    "url": r["url"], "photo": r["photo"], "category": r["category"], "likes": r["likes"],
                    "views": r["views"], "sell_s": r["sell_s"], "age_h": round(((r["checked_ts"] or r["gone_ts"] or now)
                                                                               - r["first_seen"]) / 3600, 1),
                    "when": r["checked_ts"] or r["gone_ts"]} for r in recent],
    }
