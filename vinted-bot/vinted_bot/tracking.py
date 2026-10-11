"""Suivi des annonces signalées : photos supplémentaires et disponibilité (vendue, réservée…).

Deux sources, de la moins chère à la plus chère :

1. **Disparition des résultats de recherche** (aucune requête en plus). Le catalogue ne
   renvoie que des annonces disponibles. Si une annonce signalée disparaît alors que des
   annonces plus anciennes de la même recherche sont toujours là, elle n'est plus disponible
   (vendue, réservée, masquée ou supprimée). Deux passages consécutifs sont exigés pour
   ignorer les à-coups de l'index.
2. **Page de l'annonce** (une requête, ~2 Mo) : seule source des photos supplémentaires et
   du motif exact. Ces pages sont surveillées de près par l'anti-robot de Vinted (blocage
   observé après une rafale de pages) : elles sont donc lues rarement, une à la fois, et
   jamais quand la page ressemble à un mur de connexion ou de vérification.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
from dataclasses import dataclass, field

AVAILABLE = "available"
GONE = "gone"            # disparue des recherches, motif pas encore confirmé
SOLD = "sold"
RESERVED = "reserved"
DELETED = "deleted"

STATUS_LABELS = {
    AVAILABLE: "Disponible",
    GONE: "Plus disponible",
    SOLD: "Vendue",
    RESERVED: "Réservée",
    DELETED: "Supprimée",
}

MISSING_POLLS = 2          # passages consécutifs sans l'annonce avant de la dire partie
ANCHORS = 3                # annonces plus anciennes retenues comme repères
TRACK_FOR = 48 * 3600      # durée de suivi après l'alerte


@dataclass
class Tracked:
    item_id: int
    search_key: str
    url: str
    alerted_ts: float
    status: str = AVAILABLE
    status_ts: float | None = None
    anchors: tuple[int, ...] = ()
    missing: int = 0
    handles: dict = field(default_factory=dict)  # notifier -> référence du message envoyé


class Tracker:
    def __init__(self):
        self.items: dict[int, Tracked] = {}
        self._last_seq: dict[str, int] = {}

    def add(self, item_id: int, search_key: str, url: str, alerted_ts: float | None = None,
            status: str = AVAILABLE) -> Tracked:
        t = Tracked(item_id, search_key, url, alerted_ts or time.time(), status=status)
        self.items[item_id] = t
        return t

    def expire(self, now: float | None = None) -> None:
        now = now or time.time()
        for iid in [i for i, t in self.items.items() if now - t.alerted_ts > TRACK_FOR]:
            del self.items[iid]

    def observe_page(self, search_key: str, seq: int, ids: list[int]) -> list[Tracked]:
        """`ids` : annonces non sponsorisées dans l'ordre renvoyé (plus récentes d'abord).

        Renvoie les annonces dont l'état vient de changer (GONE, ou retour à AVAILABLE).
        `seq` écarte une réponse arrivée après une réponse plus récente de la même recherche.
        """
        if seq <= self._last_seq.get(search_key, -1):
            return []
        self._last_seq[search_key] = seq
        pos = {iid: i for i, iid in enumerate(ids)}
        changed = []
        for t in self.items.values():
            if t.search_key != search_key or t.status in (SOLD, DELETED):
                continue
            if t.item_id in pos:
                i = pos[t.item_id]
                t.anchors = tuple(ids[i + 1:i + 1 + ANCHORS])
                t.missing = 0
                if t.status in (GONE, RESERVED):  # réservation annulée, ou faux départ
                    t.status, t.status_ts = AVAILABLE, time.time()
                    changed.append(t)
                continue
            if not t.anchors:
                continue  # jamais vue en position jugeable depuis le début du suivi
            if not any(a in pos for a in t.anchors):
                t.anchors = ()  # la fenêtre de résultats est passée au-delà : plus jugeable
                continue
            t.missing += 1
            if t.missing >= MISSING_POLLS and t.status == AVAILABLE:
                t.status, t.status_ts = GONE, time.time()
                changed.append(t)
        return changed


# --- lecture de la page d'une annonce ---------------------------------------------------

_WALL = re.compile(r"/(login|signup|member/general|session)|captcha|challenge|consent", re.I)
_PHOTO_TAG = re.compile(r'<img[^>]+data-testid="item-photo-(\d+)--img"[^>]*>', re.I)
_SRC = re.compile(r'\bsrc="([^"]+)"', re.I)
_F800 = re.compile(r'https://images1\.vinted\.net/t/[^"\\\s]+?/f800/[^"\\\s]+')


def _unescape(s: str) -> str:
    return html_lib.unescape(s).replace("\\u0026", "&").replace("\\/", "/")


def extract_photos(page: str, limit: int = 3) -> list[str]:
    """Photos de l'annonce, dans l'ordre de la galerie.

    1. Balises de la galerie rendue côté serveur : `<img data-testid="item-photo-N--img">`.
    2. À défaut, tableau `photos` de l'enregistrement de l'annonce dans les données Next.js
       (le premier tableau `photos` qui suit un `title` : la page contient aussi les photos
       d'autres annonces, qu'il ne faut pas mélanger).
    """
    found: dict[int, str] = {}
    for m in _PHOTO_TAG.finditer(page):
        src = _SRC.search(m.group(0))
        if src and int(m.group(1)) not in found:
            found[int(m.group(1))] = _unescape(src.group(1))
    photos = [found[k] for k in sorted(found)]
    if not photos:
        title = page.find('\\"title\\":')
        start = page.find('\\"photos\\":[', title if title >= 0 else 0)
        if start >= 0:
            depth, end = 0, start
            for end in range(start + len('\\"photos\\":'), min(len(page), start + 200_000)):
                c = page[end]
                if c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        break
            for u in _F800.findall(page[start:end]):
                u = _unescape(u)
                if u not in photos:
                    photos.append(u)
    return photos[:limit]


def page_status(status_code: int, final_url: str, page: str) -> str | None:
    """Motif d'indisponibilité lu sur la page. None = page illisible : ne rien conclure.

    Repères établis par plusieurs projets en septembre-octobre 2026 :
    - vendue : greffon `buyer_item_status` de thème SUCCESS ;
    - disponible : greffon `buy` présent ;
    - supprimée : HTTP 404/410, ou HTTP 200 portant le digest Next.js de 404 ;
    - le mot « Vendu » figure sur TOUTES les pages (traductions) : jamais utilisé seul.
    Un mur de connexion ou de vérification a déjà produit des centaines de fausses ventes
    chez d'autres : dans ce cas, aucune conclusion.
    """
    if status_code in (404, 410):
        return DELETED
    if status_code != 200 or _WALL.search(final_url or "") or "/items/" not in (final_url or "/items/"):
        return None
    if 'NEXT_HTTP_ERROR_FALLBACK;404' in page:
        return DELETED
    flat = page.replace('\\"', '"')
    for m in re.finditer(r'"name":"buyer_item_status"', flat):
        window = flat[max(0, m.start() - 600):m.end() + 600]
        if '"theme":"SUCCESS"' in window:
            return SOLD
    if re.search(r'"name":"buy"', flat) or '"availability":"InStock"' in flat:
        return AVAILABLE
    if re.search(r'"is_reserved":true', flat):
        return RESERVED  # pas de bouton d'achat et une réservation : achat en cours
    return None


def page_facts_json(page: str) -> str:
    """Résumé compact pour le journal (diagnostic)."""
    return json.dumps({"len": len(page), "photos": len(extract_photos(page, 20))})
