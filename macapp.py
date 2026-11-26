#!/usr/bin/env python3
"""Free Mail Manager as a macOS app: the same Python backend and web UI, in a native window.

webui.py serves the UI on 127.0.0.1 (random port, per-run token) and pywebview shows it in a
WKWebView. Data (config, rules, filters, backups, reports) lives in
~/Library/Application Support/FreeMail and the Free password in the macOS Keychain.

    make app-dev    # run from the sources
    make app        # build "Free Mail.app" and the .dmg (see macos/build-app.sh)
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import yaml


def _use_certifi() -> None:
    """The frozen Python has no system CA store: point OpenSSL at certifi's bundle."""
    try:
        import certifi
    except ImportError:
        return
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())


_use_certifi()

APP_NAME = "Free Mail"
SUPPORT = Path.home() / "Library" / "Application Support" / "FreeMail"
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # code + web/ (read-only when bundled)
PORT = 8777
PLACEHOLDER_USER = "prenom.nom@free.fr"
SEEDS = {"rules.example.yaml": "rules.yaml", "filters.example.yaml": "filters.yaml"}  # copied once, never overwritten


# --------------------------------------------------------------------------- native dialogs (osascript)
def _applescript(script: str) -> str | None:
    """Run an AppleScript; None when the user cancelled (or osascript failed)."""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # AppleScript accepts \" and \\ in string literals


def ask(prompt: str, default: str = "", hidden: bool = False) -> str | None:
    return _applescript(
        f'text returned of (display dialog {_q(prompt)} default answer {_q(default)} '
        f'{"with hidden answer " if hidden else ""}with title {_q(APP_NAME)} '
        'buttons {"Annuler", "OK"} default button "OK" cancel button "Annuler")')


def alert(message: str) -> None:
    _applescript(f'display dialog {_q(message)} with title {_q(APP_NAME)} buttons {{"OK"}} default button "OK" with icon caution')


# --------------------------------------------------------------------------- data directory
def prepare_home(home: Path, bundle: Path = BUNDLE) -> Path:
    """Create the data directory and seed the example rules/filters the first time."""
    home.mkdir(parents=True, exist_ok=True)
    for src, dst in SEEDS.items():
        if (bundle / src).exists() and not (home / dst).exists():
            shutil.copyfile(bundle / src, home / dst)
    return home


def repoint(fm, webui, home: Path) -> None:
    """Keep the code read-only in the bundle; write data to the support directory (same idea as the Android app)."""
    fm.ROOT, fm.OUT, fm.BACKUPS = home, home / "out", home / "backups"
    fm.BROWSER_MODE_FILE = home / ".login-browser-mode"
    webui.ROOT = home                      # webui.WEB stays inside the bundle
    webui.STATE["cfg"] = None
    os.chdir(home)


def _configured_user(home: Path) -> str | None:
    p = home / "config.yaml"
    user = (yaml.safe_load(p.read_text()) or {}).get("user") if p.exists() else None
    return user if user and user != PLACEHOLDER_USER else None


def _write_user(home: Path, user: str) -> None:
    p = home / "config.yaml"
    cfg = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
    cfg["user"] = user
    p.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def ensure_credentials(home: Path, fm, ask=ask, alert=alert) -> str | None:
    """First launch: ask for the Free address and password, test the IMAP login, store the password in the Keychain.

    Returns the address, or None when the user cancels."""
    import keyring
    user = _configured_user(home)
    if user and keyring.get_password(fm.KEYRING_SERVICE, user):
        return user
    while True:
        user = ask("Adresse mail Free (ex. prenom.nom@free.fr) :", user or "")
        if user is None:
            return None
        user = user.strip()
        pw = ask(f"Mot de passe Free de {user} (stocké dans le Trousseau macOS) :", hidden=True) if user else None
        if pw is None:
            return None
        if not user or "@" not in user or not pw:
            alert("Renseigne l'adresse complète et le mot de passe.")
            continue
        _write_user(home, user)
        try:
            fm.Mailbox(fm.load_config(str(home / "config.yaml")), password=pw).close()
        except (SystemExit, Exception) as e:  # noqa: BLE001
            alert(f"Connexion IMAP refusée : {e}")
            continue
        keyring.set_password(fm.KEYRING_SERVICE, user, pw)
        return user


# --------------------------------------------------------------------------- webmail session
def webmail_login_window(url: str, wv=None, timeout: float = 300.0, sleep=None) -> str | None:
    """Open Free's webmail in a native window, wait until the user is signed in and return the Cookie header.

    The login page needs JavaScript (no plain HTTP login) and the bundle has no Playwright: the WebView itself is the browser.
    Returns None when the window is closed or the time runs out."""
    import time
    if wv is None:
        import webview as wv  # noqa: PLC0415
    sleep = sleep or time.sleep
    closed = []
    win = wv.create_window("Connexion au webmail Free", url, width=980, height=760)
    win.events.closed += lambda *a: closed.append(1)
    deadline = time.time() + timeout
    while not closed and time.time() < deadline:
        sleep(1)
        try:
            jar = {}
            for c in win.get_cookies() or []:
                for name, m in c.items():
                    jar[name] = m.value
        except Exception:  # noqa: BLE001  (page still loading)
            continue
        if jar.get("ZM_AUTH_TOKEN"):
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
            return "; ".join(f"{k}={v}" for k, v in jar.items())
    if not closed:
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass
    return None


# --------------------------------------------------------------------------- main
def main() -> int:
    prepare_home(SUPPORT)
    import freemail as fm
    import webui
    repoint(fm, webui, SUPPORT)
    if not ensure_credentials(SUPPORT, fm):
        return 1
    webui.TOKEN = secrets.token_urlsafe(24)
    webui.STATE["macapp"] = True  # enables the in-app update (macupdate)
    webui.STATE["webmail_window"] = webmail_login_window  # Filters page: sign in to the Zimbra webmail in a window
    try:  # fixed port: the web origin (hence the settings kept in localStorage: theme, sounds...) must not change between launches
        srv = webui.start_server(PORT, imap_session=True, notify="none")
    except OSError:  # busy (e.g. `make ui` running): fall back to any free port
        srv = webui.start_server(0, imap_session=True, notify="none")
    threading.Thread(target=srv.serve_forever, daemon=True, name="fm-http").start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/?t={webui.TOKEN}"

    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True                # attachments: native save dialog
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # links in mails open in the default browser
    webview.create_window(APP_NAME, url, width=1280, height=840, min_size=(900, 600))
    webview.start(private_mode=False, storage_path=str(SUPPORT / "webview"))  # keep localStorage and cookies
    return 0


if __name__ == "__main__":
    sys.exit(main())
