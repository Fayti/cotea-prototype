"""Fenêtre Vinted pilotée par le bot, pour réserver en un clic.

Tu t'y connectes une fois à ton compte (la connexion est gardée d'un lancement à l'autre).
Pour réserver, le bot y ouvre l'annonce et touche « Acheter » à ta place : c'est le site de
Vinted lui-même qui crée la commande, avec ses propres protections, exactement comme si tu
avais cliqué. L'article est alors bloqué pour toi et la page de paiement s'affiche dans cette
fenêtre : il ne reste qu'à valider.

Processus séparé, car une fenêtre graphique exige le fil principal du programme. Protocole :
une commande JSON par ligne sur l'entrée standard, un événement JSON par ligne en sortie.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

HOME = "https://www.vinted.fr/"
RESERVE_TIMEOUT = 25.0
NO_BUTTON_AFTER = 8.0  # annonce affichée mais sans bouton « Acheter » : déjà vendue ou réservée
# Sans « Version/… Safari », certains services refusent la connexion depuis une fenêtre intégrée.
SAFARI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.5 Safari/605.1.15")

STATE_JS = r"""(() => {
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').trim().toLowerCase();
  const buy = document.querySelector('[data-testid="item-buy-button"]')
    || [...document.querySelectorAll('button')].find((b) => ['acheter', 'buy now'].includes(text(b)));
  const page = text(document.body);
  return {
    href: location.href,
    ready: document.readyState,
    buy: !!buy && !buy.disabled,
    taken: page.includes('belong to another purchase') || page.includes('en cours d’achat')
      || page.includes("en cours d'achat"),
    login: /\/(member\/signup|member\/login|signup|login)/.test(location.pathname)
      || !!document.querySelector('[data-testid*="auth-modal"], [data-testid*="auth-select"]'),
  };
})()"""

CLICK_JS = r"""(() => {
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').trim().toLowerCase();
  const buy = document.querySelector('[data-testid="item-buy-button"]')
    || [...document.querySelectorAll('button')].find((b) => ['acheter', 'buy now'].includes(text(b)));
  if (!buy) return false;
  buy.click();
  return true;
})()"""

USER_JS = r"""fetch('/api/v2/users/current', {credentials: 'include', headers: {'Accept': 'application/json'}})
  .then((r) => r.ok ? r.json() : null)
  .then((d) => (d && d.user && d.user.login) || null)
  .catch(() => null)"""


# Brouillon d'annonce, par les appels du site lui-même depuis la page connectée (cookies, jeton
# CSRF de la page) : envoi des photos, puis enregistrement du brouillon. Relevé dans le code
# public du site (24/10/2026) : jeton « CSRF_TOKEN » de la page envoyé en en-tête X-CSRF-Token.
# Les adresses d'envoi viennent du fonctionnement connu du site ; si Vinted les refuse, le
# formulaire « Vendre » est rempli à la place (FORM_JS) et tu termines toi-même.
DRAFT_JS = r"""(async (meta) => {
  const text = [...document.scripts].map((s) => s.textContent).join('\n');
  const m = text.match(/CSRF_TOKEN\\*"\s*:\s*\\*"([0-9a-f-]{36})/);
  const csrf = m ? m[1] : null;
  const headers = (json) => Object.assign({'Accept': 'application/json, text/plain, */*'},
    csrf ? {'X-CSRF-Token': csrf} : {}, json ? {'Content-Type': 'application/json'} : {});
  const uuid = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now());
  const ids = [];
  for (const b64 of (window.__vbPhotos || [])) {
    const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const fd = new FormData();
    fd.append('photo[type]', 'item');
    fd.append('photo[temp_uuid]', uuid);
    fd.append('photo[file]', new Blob([bin], {type: 'image/jpeg'}), 'photo.jpg');
    const r = await fetch('/api/v2/photos', {method: 'POST', body: fd, credentials: 'include', headers: headers(false)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.id) return {ok: false, step: 'photo', status: r.status, csrf: !!csrf, body: JSON.stringify(j).slice(0, 300)};
    ids.push(j.id);
  }
  const draft = {id: null, currency: 'EUR', temp_uuid: uuid, title: meta.title, description: meta.description,
    brand_id: meta.brand_id, brand: meta.brand, size_id: null, catalog_id: meta.catalog_id, isbn: null,
    is_unisex: null, status_id: meta.status_id, price: meta.price, color_ids: [],
    assigned_photos: ids.map((id) => ({id: id, orientation: 0})), measurement_length: null,
    measurement_width: null, item_attributes: []};
  const r = await fetch('/api/v2/item_upload/drafts', {method: 'POST', credentials: 'include', headers: headers(true),
    body: JSON.stringify({draft: draft, feedback_id: null, parcel: null, upload_session_id: uuid})});
  const j = await r.json().catch(() => ({}));
  const id = (j.draft && j.draft.id) || (j.item && j.item.id) || j.id || null;
  return {ok: r.ok && !!id, step: 'draft', status: r.status, csrf: !!csrf, draft_id: id, photos: ids.length,
          body: JSON.stringify(j).slice(0, 400)};
})(%s)"""

# Plan B : le formulaire « Vendre » de la page, rempli comme le ferait une personne (photos
# déposées dans le champ de fichier, titre, description, prix). Catégorie, état et colis : à toi.
FORM_JS = r"""((meta) => {
  const pick = (sels) => sels.map((s) => document.querySelector(s)).find(Boolean);
  const put = (el, v) => {
    if (!el || v == null) return false;
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, String(v));
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  };
  const done = {};
  const file = pick(['input[type=file][accept*=image]', 'input[type=file]']);
  if (file && window.__vbPhotos && window.__vbPhotos.length) {
    const dt = new DataTransfer();
    window.__vbPhotos.forEach((b64, i) => {
      const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      dt.items.add(new File([bin], 'photo-' + (i + 1) + '.jpg', {type: 'image/jpeg'}));
    });
    file.files = dt.files;
    file.dispatchEvent(new Event('change', {bubbles: true}));
    done.photos = dt.files.length;
  }
  done.title = put(pick(['input[name=title]', '#title', '[data-testid*=title] input']), meta.title);
  done.description = put(pick(['textarea[name=description]', '#description', '[data-testid*=description] textarea']), meta.description);
  done.price = put(pick(['input[name=price]', '#price', '[data-testid*=price] input']), meta.price);
  done.ready = !!file;
  return done;
})(%s)"""


def _run_promise(window, js: str, timeout: float = 90.0):
    done, result = threading.Event(), {}

    def callback(value):
        result["value"] = value
        done.set()

    window.evaluate_js(js, callback)
    done.wait(timeout)
    return result.get("value")


def _push_photos(window, photos_b64: list[str]) -> None:
    window.evaluate_js("window.__vbPhotos = []")
    for b64 in photos_b64:
        window.evaluate_js(f"window.__vbPhotos.push({json.dumps(b64)})")


def create_draft(window, meta: dict, photos_b64: list[str]) -> dict:
    """Brouillon sur ton compte Vinted. Renvoie {"ok": True, "draft_id"} ; sinon le formulaire
    « Vendre » est pré-rempli dans la fenêtre ({"ok": False, "prefilled": {...}})."""
    href = window.evaluate_js("location.href") or ""
    if "vinted." not in href:
        window.load_url(HOME)
        time.sleep(4)
    _push_photos(window, photos_b64)
    result = _run_promise(window, DRAFT_JS % json.dumps(meta)) or {"ok": False, "step": "script"}
    if result.get("ok"):
        return {"ok": True, "draft_id": result.get("draft_id"), "photos": result.get("photos")}
    # Plan B : formulaire « Vendre » pré-rempli.
    window.load_url(HOME.rstrip("/") + "/items/new")
    filled = {}
    for _ in range(40):  # la page met quelques secondes à se construire
        time.sleep(0.5)
        try:
            if "/items/new" in (window.evaluate_js("location.href") or "") and window.evaluate_js(
                    "!!document.querySelector('input[type=file]')"):
                _push_photos(window, photos_b64)
                filled = window.evaluate_js(FORM_JS % json.dumps(meta)) or {}
                break
        except Exception:
            continue
    login = "/member/" in (window.evaluate_js("location.href") or "")
    return {"ok": False, "api": result, "prefilled": filled, "login": login}


def current_user(window) -> str | None:
    """Pseudo du compte connecté dans la fenêtre, ou None."""
    done, result = threading.Event(), {}

    def callback(login):
        result["login"] = login if isinstance(login, str) else None
        done.set()

    window.evaluate_js(USER_JS, callback)  # promesse : le résultat arrive par le rappel
    done.wait(10)
    return result.get("login")


def emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}), flush=True)


def reserve(window, item_id: int, url: str) -> dict:
    """Ouvre l'annonce et touche « Acheter ». Réussi quand Vinted affiche la page de paiement."""
    started = time.monotonic()
    window.load_url(url)
    clicks, clicked_at, seen_at = 0, 0.0, None
    while time.monotonic() - started < RESERVE_TIMEOUT:
        time.sleep(0.15)
        try:
            state = window.evaluate_js(STATE_JS) or {}
        except Exception:
            continue
        href = state.get("href") or ""
        if "/checkout" in href:
            return {"ok": True, "checkout_url": href, "seconds": round(time.monotonic() - started, 1)}
        if state.get("taken"):
            return {"ok": False, "reason": "quelqu'un d'autre est déjà en train de l'acheter"}
        if state.get("login"):
            return {"ok": False, "reason": "connecte-toi à ton compte dans la fenêtre Vinted, puis réessaie"}
        if f"/items/{item_id}" not in href:
            continue  # l'annonce n'est pas encore affichée
        seen_at = seen_at or time.monotonic()
        if not clicks and not state.get("buy") and time.monotonic() - seen_at > NO_BUTTON_AFTER:
            break  # page affichée sans bouton « Acheter »
        # Tant que la page n'est pas « réveillée » (scripts chargés), un clic peut ne rien faire :
        # on retouche le bouton régulièrement, quelques fois.
        if state.get("buy") and clicks < 12 and time.monotonic() - clicked_at > 0.8:
            if window.evaluate_js(CLICK_JS):
                clicks, clicked_at = clicks + 1, time.monotonic()
    if seen_at is None:
        return {"ok": False, "reason": "l'annonce ne s'est pas affichée (connexion lente ?)"}
    if not clicks:
        return {"ok": False, "reason": "pas de bouton « Acheter » : annonce déjà vendue ou réservée ?"}
    return {"ok": False, "reason": "Vinted n'a pas ouvert le paiement (regarde la fenêtre Vinted)"}


def bring_to_front(window) -> None:
    for action in (window.show, window.restore):
        try:
            action()
        except Exception:
            pass
    try:
        window.on_top = True
        time.sleep(0.3)
        window.on_top = False
    except Exception:
        pass


def commands(window) -> None:
    emit("ready")
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        kind = cmd.get("cmd")
        try:
            if kind == "show":
                bring_to_front(window)
            elif kind == "user":
                emit("user", login=current_user(window))
            elif kind == "reserve":
                result = reserve(window, int(cmd["id"]), cmd["url"])
                if result["ok"]:
                    bring_to_front(window)
                emit("reserve", id=cmd["id"], **result)
            elif kind == "draft":
                result = create_draft(window, cmd["meta"], cmd.get("photos") or [])
                if not result.get("ok"):
                    bring_to_front(window)   # formulaire pré-rempli : à toi de finir
                emit("draft", id=cmd["id"], **result)
            elif kind == "quit":
                window.destroy()
                return
        except Exception as e:  # la fenêtre a pu être fermée entre-temps
            emit("error", cmd=kind, id=cmd.get("id"), message=f"{type(e).__name__}: {e}")
    # Le bot s'est arrêté : la fenêtre se ferme avec lui.
    try:
        window.destroy()
    except Exception:
        pass


def main(data_dir: Path) -> int:
    import webview

    storage = data_dir / "fenetre-vinted"
    storage.mkdir(parents=True, exist_ok=True)
    window = webview.create_window("Vinted — VintedBot", HOME, width=1150, height=900)
    window.events.closed += lambda: emit("closed")
    webview.start(commands, window, private_mode=False, storage_path=str(storage),
                  user_agent=SAFARI_UA if sys.platform == "darwin" else None)
    return 0


def probe() -> int:
    """Autotest de l'exécutable : ouvre vraiment une fenêtre, y exécute du JavaScript, la ferme."""
    import webview

    window = webview.create_window("VintedBot — test", html="<p>ok</p>", width=300, height=200)

    def check(w):
        try:
            print("WINDOW OK", w.evaluate_js("1 + 1"), flush=True)
        finally:
            w.destroy()

    webview.start(check, window, private_mode=True)
    return 0
