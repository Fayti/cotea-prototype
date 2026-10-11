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
