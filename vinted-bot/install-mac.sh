#!/bin/bash
# Installe VintedBot sur Mac et le lance. Usage, dans le Terminal :
#   curl -fsSL https://github.com/Fayti/cotea-prototype/releases/download/vinted-bot-latest/install-mac.sh | bash
#
# Le téléchargement par curl ne pose pas l'attribut de quarantaine de macOS : Gatekeeper ne
# bloque donc pas le programme (non signé), contrairement à un téléchargement via le navigateur.
set -euo pipefail

REPO="Fayti/cotea-prototype"
DIR="$HOME/VintedBot"

case "$(uname -m)" in
  arm64) ASSET="VintedBot-mac-arm64" ;;   # puces Apple M1, M2, M3, M4…
  x86_64) ASSET="VintedBot-mac-x86_64" ;; # Mac Intel (ou Terminal sous Rosetta)
  *) echo "Type de Mac non reconnu : $(uname -m)"; exit 1 ;;
esac

mkdir -p "$DIR"
echo "Téléchargement de VintedBot ($ASSET)…"
curl -fL --progress-bar "https://github.com/$REPO/releases/download/vinted-bot-latest/$ASSET" -o "$DIR/VintedBot.tmp"
chmod +x "$DIR/VintedBot.tmp"
mv -f "$DIR/VintedBot.tmp" "$DIR/VintedBot"
xattr -d com.apple.quarantine "$DIR/VintedBot" 2>/dev/null || true

# Raccourci à double-cliquer sur le Bureau. caffeinate empêche la mise en veille
# automatique tant que le bot tourne (un portable dont on ferme le capot s'endort quand même).
if [ -d "$HOME/Desktop" ]; then
  LAUNCHER="$HOME/Desktop/VintedBot.command"
  cat > "$LAUNCHER" <<'EOF'
#!/bin/bash
clear
echo "VintedBot démarre… Laisse cette fenêtre ouverte : la fermer arrête le bot."
exec caffeinate -i "$HOME/VintedBot/VintedBot"
EOF
  chmod +x "$LAUNCHER"
  echo "Installé. Pour le relancer plus tard : double-clique sur « VintedBot » sur ton Bureau."
fi

if [ -z "${VINTEDBOT_NO_LAUNCH:-}" ]; then
  echo "Lancement… L'interface va s'ouvrir dans ton navigateur."
  exec caffeinate -i "$DIR/VintedBot"
fi
