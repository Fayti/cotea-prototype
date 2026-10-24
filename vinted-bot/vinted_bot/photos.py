"""Photos d'une annonce à partir de son lien : toutes, à la plus grande taille que Vinted affiche
(800 px, relevé du 24/10/2026 : la page ne donne pas d'original plus grand), converties en JPEG
pour être réutilisables partout (Vinted, Leboncoin, galerie du téléphone).
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import re
import zipfile
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from .client import VintedClient
from .tracking import extract_photos

ITEM_URL = re.compile(r"https?://(?:www\.)?(vinted\.[a-z.]{2,8})/items/(\d+)[^\s<>\"']*", re.I)
MAX_PHOTOS = 20  # Vinted : 20 photos par annonce au plus


def parse_item_url(text: str) -> tuple[str, str] | None:
    """(domaine, lien propre) du premier lien d'annonce Vinted trouvé dans le texte, ou None."""
    m = ITEM_URL.search(text or "")
    if not m:
        return None
    return m.group(1).lower(), m.group(0).split("?")[0].split("#")[0]


def is_photo_url(url: str) -> bool:
    """Seules les images de Vinted sont téléchargées (jamais une adresse quelconque)."""
    p = urlparse(url)
    return p.scheme == "https" and (p.hostname or "").endswith(".vinted.net")


def _title(page: str) -> str:
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product" and data.get("name"):
            return str(data["name"])
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    return html.unescape(m.group(1)).replace(" | Vinted", "").strip() if m else "annonce"


async def item_photos(url: str, proxies: list[str] | None = None, use_direct: bool = True) -> dict:
    """{"title", "url", "photos": [liens]} : lit la page de l'annonce (une requête)."""
    parsed = parse_item_url(url)
    if not parsed:
        raise ValueError("Ce lien n'est pas une annonce Vinted (il doit contenir /items/).")
    domain, clean = parsed
    client = VintedClient(domain, proxies=proxies, use_direct=use_direct, request_gap=0)
    try:
        code, final_url, page = await client.get_page(clean, await client.acquire())
    finally:
        await client.close()
    if code in (404, 410):
        raise ValueError("Annonce introuvable : supprimée, ou lien incomplet.")
    if code != 200:
        raise ValueError(f"Vinted n'a pas donné la page de l'annonce (HTTP {code}). Réessaie dans un moment.")
    photos = extract_photos(page, MAX_PHOTOS)
    if not photos:
        raise ValueError("Aucune photo trouvée sur cette page (Vinted a peut-être demandé une vérification).")
    return {"title": _title(page), "url": clean, "photos": photos}


def to_jpeg(data: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95)
    return out.getvalue()


async def download_jpegs(urls: list[str]) -> list[bytes]:
    """Les photos, dans l'ordre, en JPEG. Une photo illisible est sautée."""
    urls = [u for u in urls if is_photo_url(u)][:MAX_PHOTOS]
    async with AsyncSession(impersonate="chrome", timeout=20) as http:
        async def one(u):
            resp = await http.get(u, headers={"Referer": "https://www.vinted.fr/"})
            resp.raise_for_status()
            return await asyncio.to_thread(to_jpeg, resp.content)
        results = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    return [r for r in results if isinstance(r, bytes)]


def safe_name(title: str) -> str:
    name = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip().replace(" ", "-")
    return (name or "annonce")[:60]


def make_zip(title: str, jpegs: list[bytes]) -> bytes:
    out = io.BytesIO()
    base = safe_name(title)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:   # JPEG : déjà compressé
        for i, data in enumerate(jpegs, 1):
            z.writestr(f"{base}-{i:02d}.jpg", data)
    return out.getvalue()
