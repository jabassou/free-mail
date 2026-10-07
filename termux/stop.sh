#!/data/data/com.termux/files/usr/bin/bash
# Stop the Free Mail server and release the wake lock.
pkill -f "webui.py --termux" && echo "Free Mail arrêté" || echo "Free Mail n'était pas lancé"
termux-wake-unlock 2>/dev/null || true
