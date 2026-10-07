#!/data/data/com.termux/files/usr/bin/bash
# Free Mail on Android (Termux): dependencies, stored password, auto-start at boot, home-screen widget.
# Usage: bash ~/free-mail/termux/install.sh [--reset-password]
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$HOME/.config/freemail"
say(){ printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }
ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
[ -d /data/data/com.termux ] || { echo "À lancer dans Termux sur le téléphone."; exit 1; }

say "Paquets Termux (python, termux-api, curl)"
pkg update -y >/dev/null
pkg install -y python termux-api curl >/dev/null
ok "paquets installés"

say "Bibliothèques Python"
pip install --disable-pip-version-check -q pyyaml requests || { pkg install -y clang >/dev/null; pip install --disable-pip-version-check -q pyyaml requests; }
ok "pyyaml, requests"

mkdir -p "$CONF"; chmod 700 "$CONF"
if [ ! -f "$APP/config.yaml" ]; then
  read -rp "Adresse Free (ex. prenom@free.fr) : " ADDR
  printf 'user: %s\n' "$ADDR" > "$APP/config.yaml"
fi
ADDR=$(cd "$APP" && python -c "import yaml;print(yaml.safe_load(open('config.yaml'))['user'])")

say "Mot de passe Free"
if [ ! -s "$CONF/password" ] || [ "${1:-}" = "--reset-password" ]; then
  read -rsp "Mot de passe pour $ADDR : " PW; echo
  (umask 077; printf '%s' "$PW" > "$CONF/password"); unset PW
fi
chmod 600 "$CONF/password"
ok "stocké dans $CONF/password (lisible par Termux uniquement)"

say "Test de connexion IMAP"
if (cd "$APP" && python freemail.py auth --check); then ok "connexion IMAP OK"
else echo "Échec IMAP : vérifie le mot de passe puis relance avec --reset-password"; exit 1; fi

say "Démarrage automatique (Termux:Boot) et widget (Termux:Widget)"
chmod +x "$APP"/termux/*.sh
mkdir -p "$HOME/.termux/boot" "$HOME/.shortcuts"
cat > "$HOME/.termux/boot/freemail" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
exec "$APP/termux/start.sh"
BOOT
chmod +x "$HOME/.termux/boot/freemail"
cp "$APP/termux/open.sh" "$HOME/.shortcuts/FreeMail" && chmod +x "$HOME/.shortcuts/FreeMail"
sed -i "s|^APP=.*|APP=\"$APP\"|" "$HOME/.shortcuts/FreeMail"
ok "boot : ~/.termux/boot/freemail · widget : FreeMail"

say "Test de notification"
if timeout 15 termux-notification --id freemail-test --title "Free Mail" --content "Les notifications fonctionnent ✓" --icon mail; then
  ok "notification envoyée (si rien n'apparaît : autorise les notifications de Termux:API)"
else
  echo "  ⚠ termux-notification ne répond pas : installe l'app Termux:API depuis F-Droid et autorise ses notifications."
fi

say "Lancement"
"$APP/termux/start.sh"
"$APP/termux/open.sh"
cat <<MSG

Terminé. Dans Chrome : menu ⋮ → « Installer l'application » (ou « Ajouter à l'écran d'accueil »).
Réglages Android conseillés : Applis → Termux → Batterie → « Sans restriction ».
Commandes : $APP/termux/start.sh | stop.sh | open.sh   ·   journal : $CONF/webui.log
MSG
