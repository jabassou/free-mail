#!/data/data/com.termux/files/usr/bin/bash
# Open the app (starts the server if needed). Also installed as the Termux:Widget shortcut "FreeMail".
APP="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
CONF="$HOME/.config/freemail"; PORT="${FREEMAIL_PORT:-8765}"
"$APP/termux/start.sh" >/dev/null || { termux-toast "Free Mail : échec du démarrage (voir webui.log)"; exit 1; }
termux-open-url "http://127.0.0.1:$PORT/?t=$(cat "$CONF/token")"
