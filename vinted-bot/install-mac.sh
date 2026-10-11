#!/bin/bash
# Installe (ou met à jour) VintedBot sur Mac et l'allume. Usage, dans le Terminal :
#   curl -fsSL https://github.com/Fayti/cotea-prototype/releases/download/vinted-bot-latest/install-mac.sh | bash
#
# Le téléchargement par curl ne pose pas l'attribut de quarantaine de macOS : Gatekeeper ne
# bloque donc pas le programme (non signé), contrairement à un téléchargement via le navigateur.
set -euo pipefail

REPO="Fayti/cotea-prototype"
DIR="$HOME/VintedBot"
SWITCH="$DIR/vintedbot-switch.sh"

case "$(uname -m)" in
  arm64) ASSET="VintedBot-mac-arm64" ;;   # puces Apple M1, M2, M3, M4…
  x86_64) ASSET="VintedBot-mac-x86_64" ;; # Mac Intel (ou Terminal sous Rosetta)
  *) echo "Type de Mac non reconnu : $(uname -m)"; exit 1 ;;
esac

mkdir -p "$DIR"

# Une version déjà allumée est éteinte avant la mise à jour (plus besoin de Ctrl+C).
if [ -x "$SWITCH" ]; then /bin/bash "$SWITCH" off >/dev/null 2>&1 || true; fi
pkill -f "^(caffeinate -i )?$DIR/VintedBot( |\$)" 2>/dev/null || true   # lancée par l'ancien raccourci

echo "Téléchargement de VintedBot ($ASSET)…"
curl -fL --progress-bar "https://github.com/$REPO/releases/download/vinted-bot-latest/$ASSET" -o "$DIR/VintedBot.tmp"
chmod +x "$DIR/VintedBot.tmp"
mv -f "$DIR/VintedBot.tmp" "$DIR/VintedBot"
xattr -d com.apple.quarantine "$DIR/VintedBot" 2>/dev/null || true

# Interrupteur : allume le bot en arrière-plan (aucune fenêtre Terminal à garder ouverte),
# l'éteint, ou dit s'il tourne.
cat > "$SWITCH" <<'SWITCH_END'
#!/bin/bash
BOT="$HOME/VintedBot/VintedBot"
# Seuls les processus qui démarrent par le chemin exact du bot (lancé directement ou par
# caffeinate) : jamais un autre programme dont la commande contiendrait ce nom.
PATTERN="^(caffeinate -i )?$BOT( |\$)"
case "${1:-status}" in
  status)
    if pgrep -f "$PATTERN" >/dev/null; then echo on; else echo off; fi
    ;;
  on)
    if pgrep -f "$PATTERN" >/dev/null; then
      "$BOT" >/dev/null 2>&1 &   # déjà allumé : ouvre simplement l'interface
      exit 0
    fi
    cd "$HOME/VintedBot" || exit 1
    # caffeinate empêche la mise en veille automatique tant que le bot tourne
    # (un portable dont on ferme le capot s'endort quand même).
    nohup caffeinate -i "$BOT" >> "$HOME/VintedBot/journal.txt" 2>&1 &
    ;;
  open)
    "$BOT" >/dev/null 2>&1 &     # l'instance allumée rouvre son interface dans le navigateur
    ;;
  off)
    pkill -f "$PATTERN" || exit 0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      sleep 0.5
      pgrep -f "$PATTERN" >/dev/null || exit 0
    done
    pkill -9 -f "$PATTERN" || true
    ;;
esac
exit 0
SWITCH_END
chmod +x "$SWITCH"

# Icône « VintedBot » sur le Bureau : un clic l'allume ; s'il tourne déjà, elle propose
# « Ouvrir l'interface » ou « Éteindre ». Écrite ligne par ligne (-e) pour que les accents
# passent sans souci d'encodage.
if [ -d "$HOME/Desktop" ]; then
  rm -f "$HOME/Desktop/VintedBot.command"   # ancien raccourci, remplacé par l'icône
  rm -rf "$HOME/Desktop/VintedBot.app"
  osacompile -o "$HOME/Desktop/VintedBot.app" \
    -e 'on run' \
    -e '  set sw to quoted form of (POSIX path of (path to home folder) & "VintedBot/vintedbot-switch.sh")' \
    -e '  if (do shell script "/bin/bash " & sw & " status") is "on" then' \
    -e '    set choix to button returned of (display dialog "VintedBot est allumé : il surveille Vinted." buttons {"Éteindre", "Ouvrir l’interface"} default button "Ouvrir l’interface" with title "VintedBot")' \
    -e '    if choix is "Éteindre" then' \
    -e '      do shell script "/bin/bash " & sw & " off"' \
    -e '      display notification "Reclique sur VintedBot pour le rallumer." with title "VintedBot est éteint"' \
    -e '    else' \
    -e '      do shell script "/bin/bash " & sw & " open"' \
    -e '    end if' \
    -e '  else' \
    -e '    do shell script "/bin/bash " & sw & " on"' \
    -e '    display notification "Il surveille Vinted. L’interface s’ouvre dans ton navigateur." with title "VintedBot est allumé"' \
    -e '  end if' \
    -e 'end run'
  echo "Installé. Sur ton Bureau, l'icône « VintedBot » allume et éteint le bot."
fi

if [ -z "${VINTEDBOT_NO_LAUNCH:-}" ]; then
  /bin/bash "$SWITCH" on
  echo "VintedBot est allumé : l'interface va s'ouvrir dans ton navigateur."
  echo "Tu peux fermer cette fenêtre Terminal : le bot continue de tourner."
fi
