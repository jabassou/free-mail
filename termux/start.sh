#!/data/data/com.termux/files/usr/bin/bash
# Start the Free Mail server in the background (idempotent).
APP="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
CONF="$HOME/.config/freemail"; PORT="${FREEMAIL_PORT:-8765}"; LOG="$CONF/webui.log"
mkdir -p "$CONF"
up(){ curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/manifest.webmanifest"; }
termux-wake-lock 2>/dev/null || true
if up; then echo "Free Mail tourne déjà (port $PORT)"; exit 0; fi
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ] && mv -f "$LOG" "$LOG.1"
cd "$APP" || exit 1
nohup python webui.py --termux --port "$PORT" --notify-interval "${FREEMAIL_INTERVAL:-60}" >> "$LOG" 2>&1 &
for _ in $(seq 1 30); do up && { echo "Free Mail démarré (port $PORT)"; exit 0; }; sleep 0.5; done
echo "Échec du démarrage, extrait du journal $LOG :"; tail -n 20 "$LOG"; exit 1
