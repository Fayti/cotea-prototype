"""IA intégrée (Claude, d'Anthropic) : elle regarde tes photos et rédige l'annonce optimisée.

Elle identifie l'article (modèle exact, stockage, accessoires visibles), juge l'état sur les
photos, écrit un titre qui reprend les mots que les acheteurs tapent dans la recherche Vinted,
une description honnête et rassurante, propose un prix d'après les ventes vues par le bot, et
choisit la photo de couverture la plus vendeuse.

Il faut une clé d'API Anthropic (console.anthropic.com), payée à l'usage : quelques centimes
par annonce. Les photos (réduites à 1024 px, sans métadonnées) sont envoyées à Anthropic.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_PHOTOS = 8        # au-delà, l'IA n'apprend presque rien de plus et la facture monte
PHOTO_SIDE = 1024     # ~1 000 jetons par photo

SYSTEM = """Tu rédiges des annonces Vinted qui se vendent vite, pour un revendeur de consoles \
(surtout des PS5) en France. Tu reçois les photos de l'article, parfois quelques mots du vendeur, \
et les prix de vente récents observés sur Vinted.

Identifier l'article :
- Modèle exact : PS5 Pro, PS5 Slim ou PS5 « fat » (première version) ; édition Standard (avec \
lecteur de disque) ou Digital (sans lecteur). La PS5 Pro n'a pas de lecteur intégré. Stockage \
(825 Go, 1 To, 2 To) seulement s'il est visible ou indiqué.
- Ce qui est visible : nombre et couleur des manettes, câbles, boîte d'origine, jeux (titres \
lisibles), facture.
- L'état vu sur les photos : rayures, traces, poussière, jaunissement, défauts.

Règles absolues :
- N'affirme rien qui ne soit ni visible ni indiqué par le vendeur (facture, garantie, « testée », \
« jamais servie »…). En cas de doute, reste général.
- Jamais de moyen de contact ou de paiement hors Vinted, ni de numéro, ni de formule qui \
ressemble à une arnaque (« me contacter avant d'acheter », « paiement uniquement… »).
- Français simple et naturel, sans emoji, sans majuscules criardes, sans « urgent ».

Titre (60 caractères au plus) : commence par le nom que les acheteurs tapent (« PS5 Pro », \
« PS5 Slim Digital », « PS5 Slim Standard »…), puis le stockage s'il est connu, puis le plus \
important (« + 2 manettes », « boîte »). Exemple : « PS5 Slim Standard 1 To + 2 manettes, boîte ».

Description (6 à 10 lignes courtes) : modèle et état honnête ; contenu exact du lot ; ce qui \
rassure l'acheteur (seulement ce qui est vrai) ; défauts visibles mentionnés franchement ; \
envoi soigné et bien protégé. Termine par une ligne de 3 à 6 mots-clés utiles à la recherche, \
séparés par des virgules (ex. « PlayStation 5, console Sony, DualSense »).

Prix : propose le prix de mise en vente qui part en quelques jours, d'après les ventes récentes \
fournies et l'état (un peu sous la médiane si l'état est moyen ou s'il manque la boîte). Sans \
données de vente pour ce modèle, propose une estimation prudente et dis-le dans price_reason.

Photo de couverture : l'indice (à partir de 0) de la photo la plus vendeuse (console entière, de \
face, nette, bien éclairée). Conseils : 1 à 3 conseils courts et concrets pour mieux vendre \
(photo qui manque, information à ajouter)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "model": {"type": "string", "enum": ["PS5 Pro", "PS5 Digital", "PS5 avec lecteur", "autre"]},
        "item": {"type": "string"},
        "condition": {"type": "string", "enum": ["neuf", "comme_neuf", "tres_bon", "bon"]},
        "controllers": {"type": "integer"},
        "box": {"type": "boolean"},
        "invoice": {"type": "boolean"},
        "slim": {"type": "boolean"},
        "storage": {"type": "string", "enum": ["", "825 Go", "1 To", "2 To"]},
        "games": {"type": "string"},
        "defects": {"type": "string"},
        "price": {"type": "number"},
        "price_reason": {"type": "string"},
        "cover_index": {"type": "integer"},
        "tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "model", "item", "condition", "controllers", "box", "invoice", "slim",
                 "storage", "games", "defects", "price", "price_reason", "cover_index", "tips"],
    "additionalProperties": False,
}


class AIError(Exception):
    pass


def available(api_key: str | None) -> bool:
    return bool(api_key or os.environ.get("ANTHROPIC_API_KEY"))


def _client(api_key: str | None):
    import anthropic
    return anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()


def _small(jpeg: bytes) -> str:
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg))
    img.thumbnail((PHOTO_SIDE, PHOTO_SIDE))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=85)
    return base64.standard_b64encode(out.getvalue()).decode()


def _explain(e: Exception) -> str:
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "clé d'API refusée : recopie-la depuis console.anthropic.com"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "clé d'API sans droit d'utiliser ce modèle"
    if isinstance(e, anthropic.RateLimitError):
        return "trop de demandes à l'IA en même temps : réessaie dans une minute"
    if isinstance(e, anthropic.BadRequestError):
        msg = str(getattr(e, "message", e))
        if "credit" in msg.lower() or "billing" in msg.lower():
            return "plus de crédit sur ton compte Anthropic : ajoute du crédit sur console.anthropic.com"
        return f"demande refusée par l'IA ({msg[:120]})"
    if isinstance(e, anthropic.APIStatusError):
        return f"IA indisponible pour l'instant (erreur {e.status_code}) : réessaie plus tard"
    if isinstance(e, anthropic.APIConnectionError):
        return "pas de connexion à l'IA (internet ?)"
    return f"{type(e).__name__}: {e}"


async def check_key(api_key: str | None) -> str:
    """Vérifie la clé sans rien générer (aucun coût). Renvoie le nom du modèle."""
    try:
        model = await _client(api_key).models.retrieve(MODEL)
        return getattr(model, "display_name", None) or MODEL
    except Exception as e:
        raise AIError(_explain(e)) from e


async def write_listing(api_key: str | None, photos: list[bytes], hints: str, fields: dict,
                        market: list[str]) -> dict:
    """Annonce rédigée par l'IA à partir des photos (JPEG nettoyés). Lève AIError en cas d'échec."""
    content: list[dict] = []
    for i, jpeg in enumerate(photos[:MAX_PHOTOS]):
        content.append({"type": "text", "text": f"Photo {i}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": _small(jpeg)}})
    known = {k: v for k, v in fields.items() if v not in (None, "", False, 0)}
    content.append({"type": "text", "text": (
        f"Quelques mots du vendeur : {hints.strip() or '(aucun)'}\n"
        f"Ce que le vendeur a déjà indiqué : {json.dumps(known, ensure_ascii=False) if known else '(rien)'}\n"
        "Ventes récentes observées sur Vinted : " + ("; ".join(market) if market else "(aucune donnée)") + "\n"
        "Rédige l'annonce.")})
    try:
        response = await _client(api_key).beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            # Si un filtre de sécurité refusait la demande, un autre modèle la reprend.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception as e:
        raise AIError(_explain(e)) from e
    if response.stop_reason == "refusal":
        raise AIError("l'IA a refusé de rédiger cette annonce")
    if response.stop_reason == "max_tokens":
        raise AIError("réponse de l'IA incomplète : réessaie")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise AIError("réponse de l'IA illisible : réessaie") from e
    usage = getattr(response, "usage", None)
    if usage is not None:
        log.info("Annonce rédigée par l'IA (%s jetons lus, %s écrits)", usage.input_tokens, usage.output_tokens)
    return data
