#!/usr/bin/env bash
# Build the signed Free Mail APK on macOS (or Linux). First run downloads the Android SDK (~1 GB)
# and Gradle; later builds take ~1 min. Output: ../FreeMail.apk
#   ./build-apk.sh            build
#   ./build-apk.sh --install  build + install on the phone over USB/Wi-Fi debugging (adb)
# Also used by GitHub Actions (.github/workflows/apk.yml): JAVA_HOME / ANDROID_HOME / python3.12 come
# from the runner and the signing key from repository secrets.
set -euo pipefail
cd "$(dirname "$0")"
ANDROID_DIR="$PWD"; PROJECT_DIR="$(cd .. && pwd)"
say(){ printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

say "Java 17+"
java_ok(){ "$1/bin/java" -version 2>&1 | awk -F'"' '/version/{split($2,v,".");exit !(v[1]>=17)}'; }
if [ -n "${JAVA_HOME:-}" ] && java_ok "$JAVA_HOME"; then :
elif [ -x /usr/libexec/java_home ] && JH=$(/usr/libexec/java_home -v 17+ 2>/dev/null) && java_ok "$JH"; then export JAVA_HOME="$JH"
elif [ -d "/Applications/Android Studio.app/Contents/jbr/Contents/Home" ]; then export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
elif command -v brew >/dev/null; then brew install --cask temurin@21; export JAVA_HOME=$(/usr/libexec/java_home -v 21)
else die "Installe un JDK 17+ (ex. brew install --cask temurin@21)"; fi
echo "JAVA_HOME=$JAVA_HOME"

say "Android SDK"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
[ "$(uname)" = "Linux" ] && [ -z "${ANDROID_HOME:-}" ] && SDK="$HOME/Android/Sdk"
SDKM="$SDK/cmdline-tools/latest/bin/sdkmanager"
[ ! -x "$SDKM" ] && command -v sdkmanager >/dev/null && SDKM="$(command -v sdkmanager)"
if [ ! -x "$SDKM" ]; then
  OS=$([ "$(uname)" = "Darwin" ] && echo mac || echo linux)
  URL=$(curl -fsSL https://dl.google.com/android/repository/repository2-3.xml | python3 -c "
import sys,re
x=sys.stdin.read()
m=re.search(r'<remotePackage path=\"cmdline-tools;latest\">.*?</remotePackage>',x,re.S).group(0)
print('https://dl.google.com/android/repository/'+re.search(r'<url>(commandlinetools-$OS-[^<]+)</url>',m).group(1))")
  echo "téléchargement $URL"
  TMP=$(mktemp -d); curl -fL --progress-bar "$URL" -o "$TMP/clt.zip"
  mkdir -p "$SDK/cmdline-tools"; rm -rf "$SDK/cmdline-tools/latest"
  unzip -q "$TMP/clt.zip" -d "$TMP" && mv "$TMP/cmdline-tools" "$SDK/cmdline-tools/latest"; rm -rf "$TMP"
fi
yes | "$SDKM" --sdk_root="$SDK" --licenses >/dev/null || true
"$SDKM" --sdk_root="$SDK" --install "platform-tools" "platforms;android-35" "build-tools;35.0.0" >/dev/null
echo "sdk.dir=$SDK" > local.properties
export ANDROID_HOME="$SDK"

say "Python pour Chaquopy (3.10 à 3.13)"
PY=""; PYV=""
for v in 3.12 3.13 3.11 3.10; do
  if command -v "python$v" >/dev/null; then PY="$(command -v python$v)"; PYV="$v"; break; fi
done
if [ -z "$PY" ] && command -v brew >/dev/null; then brew install python@3.12; PY="$(brew --prefix)/bin/python3.12"; PYV=3.12; fi
[ -n "$PY" ] || die "Il faut python3.10..3.13 (ex. brew install python@3.12)"
echo "$PY ($PYV)"

say "Sources (backend Python + interface web)"
cp "$PROJECT_DIR"/freemail.py "$PROJECT_DIR"/mailweb.py "$PROJECT_DIR"/webui.py "$PROJECT_DIR"/extras.py app/src/main/python/
rm -rf app/src/main/assets/web && mkdir -p app/src/main/assets && cp -R "$PROJECT_DIR/web" app/src/main/assets/web
find app/src/main/assets/web -name '.DS_Store' -delete

say "Clé de signature"
if [ ! -f signing/keystore.properties ] && [ -n "${CI:-}" ]; then
  die "signing/keystore.properties manquant : configure les secrets ANDROID_KEYSTORE_* (voir android/signing-backup.sh)"
fi
if [ ! -f signing/keystore.properties ]; then
  mkdir -p signing; PASS=$(openssl rand -base64 24 | tr -d '/+=')
  "$JAVA_HOME/bin/keytool" -genkeypair -noprompt -keystore signing/freemail-release.jks -alias freemail -keyalg RSA -keysize 4096 \
    -validity 10950 -storepass "$PASS" -keypass "$PASS" -dname "CN=Free Mail, O=jabassou, C=FR"
  printf 'storeFile=signing/freemail-release.jks\nstorePassword=%s\nkeyAlias=freemail\nkeyPassword=%s\n' "$PASS" "$PASS" > signing/keystore.properties
  chmod 600 signing/*
  echo "Nouvelle clé créée : sauvegarde-la avec ./signing-backup.sh (sans elle, impossible de mettre à jour l'appli installée)"
else echo "clé existante : signing/freemail-release.jks"; fi

say "Compilation (assembleRelease)"
VC=$(date +%y%m%d%H)                       # always increasing -> Android accepts the update
VN="$(tr -d ' \n' < "$PROJECT_DIR/VERSION" 2>/dev/null || echo 1.0.0)"   # bump VERSION at the repo root
./gradlew --no-daemon ${CI:+--console=plain} assembleRelease -PpyVersion="$PYV" -PbuildPython="$PY" -PversionCode="$VC" -PversionName="$VN"

APK=app/build/outputs/apk/release/app-release.apk
[ -f "$APK" ] || die "APK introuvable"
"$SDK/build-tools/35.0.0/apksigner" verify --print-certs "$APK" | head -3
cp "$APK" "$PROJECT_DIR/FreeMail.apk"
say "OK : $PROJECT_DIR/FreeMail.apk ($(du -h "$PROJECT_DIR/FreeMail.apk" | cut -f1)) - version $VN, build $VC"

if [ "${1:-}" = "--install" ]; then
  "$SDK/platform-tools/adb" install -r "$PROJECT_DIR/FreeMail.apk"
fi
