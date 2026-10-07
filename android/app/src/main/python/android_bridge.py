"""Android entry points, called from Kotlin through Chaquopy.

The same Python backend as on the Mac runs inside the app: webui.py serves the web UI and the
JSON API on 127.0.0.1 (random port, per-install token) for the WebView, and its Notifier posts
Android notifications through fr.jabassou.freemail.Bridge.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path

_lock = threading.Lock()
_srv = None
_mods: dict = {}


def _setup(home: str, user: str):
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass
    h = Path(home)
    h.mkdir(parents=True, exist_ok=True)
    (h / "config.yaml").write_text(f"user: {user}\n")
    import freemail as fm
    import mailweb
    import webui
    # keep code read-only in the APK; data (reports, backups, rules) lives in the app's files dir
    fm.ROOT, fm.OUT, fm.BACKUPS = h, h / "out", h / "backups"
    webui.ROOT, webui.WEB = h, h / "web"
    webui.STATE["cfg"] = None
    _mods.update(fm=fm, webui=webui, mailweb=mailweb)
    return fm, webui


def test_login(home: str, user: str, password: str) -> str:
    """'' when the IMAP login works, else a readable error."""
    fm, _ = _setup(home, user)
    try:
        cfg = fm.load_config(str(Path(home) / "config.yaml"))
        fm.Mailbox(cfg, password=password).close()
        return ""
    except SystemExit as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg.upper() or "LOGIN" in msg.upper():
            return "AUTH:refused"
        return f"{type(e).__name__}: {msg}"


def start(home: str, user: str, password: str, token: str, interval: int, lang: str = "fr", version: str = "") -> int:
    """Start (once) the local server + notifier; returns the HTTP port."""
    global _srv
    with _lock:
        if _srv is not None:
            return _srv.server_address[1]
        os.environ["FREEMAIL_PASSWORD"] = password  # process memory only
        os.environ["FREEMAIL_LANG"] = lang or "fr"  # notification texts follow the phone language
        _, webui = _setup(home, user)
        webui.TOKEN = token
        webui.STATE["android"] = True
        if version:
            webui.APP_VERSION = version  # VERSION is not shipped in the APK: use the app versionName
        _srv = webui.start_server(0, imap_session=True, notify="android", interval=int(interval))
        threading.Thread(target=_srv.serve_forever, daemon=True, name="fm-http").start()
        return _srv.server_address[1]


def port() -> int:
    return _srv.server_address[1] if _srv else 0


def set_password(password: str):
    os.environ["FREEMAIL_PASSWORD"] = password
    if "webui" in _mods:
        _mods["webui"].MAIL.reset()


def kick():
    w = _mods.get("webui")
    if w and w.NOTIFIER[0]:
        w.NOTIFIER[0].kick()


def check_now() -> bool:
    """Synchronous mailbox check (alarm receiver holds a wake lock meanwhile)."""
    w = _mods.get("webui")
    if w and w.NOTIFIER[0]:
        return w.NOTIFIER[0].check_now()
    return False


def set_foreground(on: bool):
    """The app UI is on screen (True) or not; notifications are skipped only while it is."""
    w = _mods.get("webui")
    if w:
        w.STATE["foreground"] = bool(on)


def mark_seen(folder: str, uid: str) -> bool:
    w, mw = _mods["webui"], _mods["mailweb"]
    w.mail(lambda mb: mw.set_flag(mb, folder, [uid], "seen", True))
    w.seen_on_device(folder, [uid])
    return True


def quick_reply(folder: str, uid: str, text: str) -> bool:
    """Reply typed in the notification (RemoteInput)."""
    w = _mods["webui"]
    import extras
    w.mail(lambda mb: extras.quick_reply(w.cfg(), w._send_password(), mb, folder, uid, text))
    w.seen_on_device(folder, [uid])
    return True


def zimbra_login(cookie: str) -> str:
    """Hand the webmail session cookie (captured by the login WebView) to the server. JSON result."""
    w = _mods["webui"]
    req = urllib.request.Request(f"http://127.0.0.1:{port()}/api/login", data=json.dumps({"cookie": cookie}).encode(),
                                 headers={"X-Token": w.TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode() or json.dumps({"error": f"HTTP {e.code}"})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
