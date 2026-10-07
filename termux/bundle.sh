#!/usr/bin/env bash
# Run on the Mac: builds free-mail-termux.zip to copy to the phone.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="free-mail-termux.zip"
rm -f "$OUT"
zip -qr "$OUT" freemail.py mailweb.py webui.py extras.py VERSION config.yaml rules.yaml requirements.txt web termux -x '*/__pycache__/*' '*.DS_Store' 'termux/bundle.sh'
echo "$OUT prêt ($(du -h "$OUT" | cut -f1)). Copie-le dans Téléchargements sur le téléphone :"
echo "  - USB / Google Drive, ou : adb push $OUT /sdcard/Download/"
