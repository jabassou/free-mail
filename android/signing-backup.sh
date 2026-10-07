#!/usr/bin/env bash
# Sauvegarde la clé de signature de l'APK et (option) la charge dans les secrets GitHub Actions.
# Sans cette clé, aucune mise à jour de l'appli installée n'est possible (Android refuse une autre signature).
#   ./signing-backup.sh                 archive chiffrée dans ~/Documents (mot de passe demandé)
#   ./signing-backup.sh --github        + secrets ANDROID_KEYSTORE_* du dépôt courant (gh CLI)
set -euo pipefail
cd "$(dirname "$0")"
P=signing/keystore.properties
[ -f "$P" ] || { echo "Pas encore de clé : lance ./build-apk.sh une première fois." >&2; exit 1; }
get(){ sed -n "s/^$1=//p" "$P"; }

OUT="$HOME/Documents/free-mail-signing-$(date +%Y%m%d).tar.gz.enc"
tar -czf - -C signing . | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -out "$OUT"
chmod 600 "$OUT"
echo "Archive chiffrée : $OUT"
echo "  restaurer : openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in \"$OUT\" | tar -xzf - -C android/signing"
echo "  garde-la hors du Mac (gestionnaire de mots de passe, clé USB, Drive...)."

if [ "${1:-}" = "--github" ]; then
  command -v gh >/dev/null || { echo "gh CLI manquant (brew install gh)" >&2; exit 1; }
  cd ..
  base64 < "android/$(get storeFile)" | tr -d '\n' | gh secret set ANDROID_KEYSTORE_B64
  get storePassword | gh secret set ANDROID_KEYSTORE_PASSWORD
  get keyAlias      | gh secret set ANDROID_KEY_ALIAS
  get keyPassword   | gh secret set ANDROID_KEY_PASSWORD
  echo "Secrets GitHub configurés pour $(gh repo view --json nameWithOwner -q .nameWithOwner)"
fi
