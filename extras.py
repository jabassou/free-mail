"""Server-side features shared by the desktop UI and the Android app.

- settings.json   user preferences (display name, signature, notifications, push folders)
- diagnostic log  in-memory ring buffer shown in Settings > Diagnostic
- outbox.json     scheduled sends (also used by "undo send" on the client side)
- snooze.json     mails hidden in a folder until a date, then moved back as unread
- contacts        address book built from Sent recipients and Inbox senders
- quick reply     reply from an Android notification (RemoteInput)
"""
from __future__ import annotations

import collections
import datetime as dt
import email
import email.policy
import html as htmlmod
import json
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

import freemail as fm
import mailweb

# --------------------------------------------------------------------------- diagnostic log
LOG: collections.deque = collections.deque(maxlen=400)


def diag(msg: str, level: str = "info"):
    LOG.append({"t": time.time(), "level": level, "msg": str(msg)[:600]})


def diag_exc(where: str, e: BaseException):
    diag(f"{where}: {type(e).__name__}: {e}", "error")
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__)[-3:])
    LOG.append({"t": time.time(), "level": "trace", "msg": tb[-1200:]})


# --------------------------------------------------------------------------- settings
DEFAULTS = {
    "from_name": "",
    "signature": "",            # HTML
    "notify_off": [],           # folder names that never notify
    "quiet": {"on": False, "start": "22:00", "end": "07:00"},
    "push_folders": ["INBOX"],  # IMAP IDLE (instant) - max 5 connections
    "update_skip": "",          # release version the user chose to skip (no prompt, no notification)
    "undo_seconds": 8,
}
_lock = threading.RLock()


class Store:
    """Small JSON files next to config.yaml (project dir on the Mac, app files dir on Android)."""

    def __init__(self, root_fn):
        self.root_fn = root_fn

    def path(self, name: str) -> Path:
        return Path(self.root_fn()) / name

    def load(self, name: str, default):
        try:
            return json.loads(self.path(name).read_text())
        except Exception:  # noqa: BLE001
            return json.loads(json.dumps(default))

    def save(self, name: str, data):
        p = self.path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(p)


STORE: Store | None = None


def init(root_fn):
    global STORE
    STORE = Store(root_fn)


def get_settings() -> dict:
    with _lock:
        s = STORE.load("settings.json", {})
        out = json.loads(json.dumps(DEFAULTS))
        out.update({k: v for k, v in s.items() if k in DEFAULTS})
        if isinstance(s.get("quiet"), dict):
            out["quiet"] = {**DEFAULTS["quiet"], **s["quiet"]}
        return out


def save_settings(patch: dict) -> dict:
    with _lock:
        cur = get_settings()
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                continue
            if k == "push_folders":
                v = [str(x) for x in (v or [])][:5]  # empty = no instant push (periodic checks only)
            if k == "notify_off":
                v = [str(x) for x in (v or [])]
            if k == "quiet":
                v = {**cur["quiet"], **{a: b for a, b in (v or {}).items() if a in ("on", "start", "end")}}
            if k == "signature":
                v = mailweb._clean_html(str(v or ""))[:20000]
            if k == "undo_seconds":
                v = max(0, min(30, int(v or 0)))
            if k == "update_skip":
                v = re.sub(r"[^\w.-]", "", str(v or ""))[:40]
            cur[k] = v
        STORE.save("settings.json", cur)
        diag("settings saved")
        return cur


def in_quiet_hours(now: dt.datetime | None = None) -> bool:
    q = get_settings()["quiet"]
    if not q.get("on"):
        return False
    now = now or dt.datetime.now()
    try:
        s = dt.time.fromisoformat(q["start"])
        e = dt.time.fromisoformat(q["end"])
    except ValueError:
        return False
    t = now.time()
    return s <= t < e if s <= e else (t >= s or t < e)


# --------------------------------------------------------------------------- scheduler (outbox + snooze)
SNOOZE_FOLDER = "En attente"


def _parse_when(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.astimezone()
    return d.timestamp()


def schedule_send(data: dict, when) -> dict:
    ts = _parse_when(when)
    if ts < time.time() - 60:
        raise mailweb.MailError("La date d'envoi est déjà passée.")
    item = {"id": uuid.uuid4().hex[:12], "at": ts, "data": data,
            "summary": {"to": (data.get("to") or [])[:3], "subject": data.get("subject") or ""}}
    with _lock:
        box = STORE.load("outbox.json", [])
        box.append(item)
        STORE.save("outbox.json", box)
    diag(f"send scheduled {item['id']} at {dt.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}")
    return {"ok": True, "id": item["id"], "at": ts}


def list_scheduled() -> list[dict]:
    return [{"id": x["id"], "at": x["at"], **x["summary"], "error": x.get("error"),
             "sending": bool(x.get("sending")), "tries": x.get("tries", 0)} for x in STORE.load("outbox.json", [])]


def cancel_scheduled(item_id: str) -> dict:
    with _lock:
        box = STORE.load("outbox.json", [])
        gone = [x for x in box if x["id"] == item_id and not x.get("sending")]
        if not gone:
            return {"ok": False, "message": None}
        STORE.save("outbox.json", [x for x in box if x["id"] != item_id])
    return {"ok": bool(gone), "message": gone[0]["data"] if gone else None}


def reschedule(item_id: str, when) -> dict:
    ts = _parse_when(when)
    with _lock:
        box = STORE.load("outbox.json", [])
        hit = False
        for x in box:
            if x["id"] == item_id:
                x["at"], hit = ts, True
                x.pop("error", None)
                x.pop("tries", None)
        STORE.save("outbox.json", box)
    return {"ok": hit}


# --------------------------------------------------------------------------- updates
def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


UPDATE_REPO = "jabassou/free-mail"   # releases published by .github/workflows/apk.yml
UPDATE_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_EVERY = 30 * 60               # background check period; conditional requests (ETag, 304) cost no API quota
_upd_lock = threading.Lock()


def _http_get(url: str, headers: dict) -> tuple[int, dict, bytes]:
    """(status, headers, body); 304 is a normal answer here, not an error."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "free-mail", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, dict(e.headers), b""
        raise


def check_update(current: str, force: bool = False) -> dict:
    """Latest release of the app, cached UPDATE_EVERY seconds (ETag: an unchanged release costs no quota)."""
    with _upd_lock:
        st = STORE.load("update.json", {})
        if force or not st.get("release") or time.time() - st.get("checked", 0) > UPDATE_EVERY:
            headers = {"Accept": "application/vnd.github+json"}
            if st.get("etag") and st.get("release"):
                headers["If-None-Match"] = st["etag"]
            try:
                code, h, body = _http_get(UPDATE_API, headers)
                if code == 200:
                    rel = json.loads(body)
                    st["release"] = {
                        "tag": rel.get("tag_name") or "", "url": rel.get("html_url"),
                        "notes": (rel.get("body") or "")[:4000],
                        "apk": next((a["browser_download_url"] for a in rel.get("assets", [])
                                     if a.get("name", "").endswith(".apk")), None)}
                    st["etag"] = h.get("ETag") or h.get("etag")
            except Exception as e:  # noqa: BLE001
                diag(f"update check failed: {e}", "warn")
                st["checked"] = time.time()  # back off until the next period
                STORE.save("update.json", st)
                if not st.get("release"):
                    raise mailweb.MailError(f"Vérification impossible : {e}") from None
            st["checked"] = time.time()
            STORE.save("update.json", st)
        rel = st.get("release") or {}
    version = (rel.get("tag") or "").lstrip("v")
    skip = get_settings().get("update_skip") or ""
    return {"current": current, "version": version, "available": bool(version) and _ver(version) > _ver(current),
            "skipped": bool(skip) and skip == version, "apk": rel.get("apk"), "url": rel.get("url"),
            "notes": rel.get("notes") or "", "checked": st.get("checked"), "repo": UPDATE_REPO}


def update_to_announce(current: str) -> dict | None:
    """For the background notifier: a newer, not skipped release, returned once per version."""
    try:
        r = check_update(current)
    except Exception:  # noqa: BLE001
        return None
    if not r["available"] or r["skipped"]:
        return None
    with _upd_lock:
        st = STORE.load("update.json", {})
        if st.get("announced") == r["version"]:
            return None
        st["announced"] = r["version"]
        STORE.save("update.json", st)
    diag(f"update available: {r['version']}")
    return r


def snooze(mb: fm.Mailbox, folder: str, uids, until) -> dict:
    ts = _parse_when(until)
    mailweb._refresh(mb)
    if not mb.exists(SNOOZE_FOLDER):
        mb.M.create(fm.quote(fm.utf7_encode(SNOOZE_FOLDER)))
        mailweb._refresh(mb)
    mb.select(mb.resolve(folder))
    uids = [str(int(u)) for u in uids]
    typ, d = mb.M.uid("FETCH", ",".join(uids), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)])")
    entries = []
    for it in d or []:
        if isinstance(it, tuple):
            h = email.message_from_bytes(it[1])
            mid = (h.get("Message-ID") or "").strip()
            if mid:
                entries.append({"msgid": mid, "until": ts, "back": folder, "subject": mailweb._dec(h.get("Subject"))})
    mb.select(mb.resolve(folder), readonly=False)
    mb.move(uids, mb.resolve(SNOOZE_FOLDER))
    with _lock:
        sz = STORE.load("snooze.json", [])
        sz += entries
        STORE.save("snooze.json", sz)
    diag(f"snoozed {len(entries)} mail(s) until {dt.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}")
    return {"ok": True, "count": len(entries), "until": ts, "folder": SNOOZE_FOLDER}


def list_snoozed() -> list[dict]:
    return STORE.load("snooze.json", [])


def _wake_snoozed(mb: fm.Mailbox, entries: list[dict]) -> list[dict]:
    """Move due snoozed mails back (unread). Returns entries that could not be found yet."""
    left = []
    mailweb._refresh(mb)
    if not mb.exists(SNOOZE_FOLDER):
        return []
    for e in entries:
        mb.select(mb.resolve(SNOOZE_FOLDER), readonly=False)
        mid = re.sub(r'["\\\r\n]', "", e["msgid"])
        found = mb.search(f'HEADER Message-ID "{mid}"')
        if not found:
            continue  # moved/deleted by the user meanwhile
        mb.M.uid("STORE", ",".join(found), "-FLAGS.SILENT", r"(\Seen)")
        dest = e["back"] if mb.exists(e["back"]) else "INBOX"
        mb.move(found, mb.resolve(dest))
        diag(f"snooze over: “{e.get('subject', '')[:60]}” back in {dest}")
    return left


class Scheduler(threading.Thread):
    """Sends due scheduled mails and brings snoozed mails back. Runs every 30 s (and on kick)."""

    def __init__(self, cfg_fn, pw_fn, mail_fn, after_fn=None):
        super().__init__(daemon=True, name="fm-scheduler")
        self.cfg_fn, self.pw_fn, self.mail_fn, self.after_fn = cfg_fn, pw_fn, mail_fn, after_fn
        self.wake = threading.Event()

    def kick(self):
        self.wake.set()

    def run(self):
        with _lock:  # a send interrupted by an app kill is retried
            box = STORE.load("outbox.json", [])
            if any(x.get("sending") for x in box):
                STORE.save("outbox.json", [{**x, "sending": False} for x in box])
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                diag_exc("scheduler", e)
            self.wake.wait(self._next_wait())
            self.wake.clear()

    @staticmethod
    def _next_wait() -> float:
        """Sleep until the next due item (undo-send delays are a few seconds), at most 30 s."""
        try:
            pending = [x["at"] for x in STORE.load("outbox.json", []) if not x.get("error")]
            pending += [x["until"] for x in STORE.load("snooze.json", [])]
        except Exception:  # noqa: BLE001
            pending = []
        nxt = min(pending, default=time.time() + 30) - time.time()
        return max(0.5, min(30.0, nxt + 0.2))

    def tick(self):
        now = time.time()
        changed = False
        with _lock:
            box = STORE.load("outbox.json", [])
            due = [x for x in box if x["at"] <= now and not x.get("error")]
            for x in due:  # "undo send" can no longer cancel these
                x["sending"] = True
            if due:
                STORE.save("outbox.json", box)
        for item in due:
            try:
                self.mail_fn(lambda mb: mailweb.send(self.cfg_fn(), self.pw_fn(), mb, item["data"]))
                diag(f"scheduled mail sent: “{item['summary'].get('subject', '')[:60]}”")
                with _lock:
                    STORE.save("outbox.json", [x for x in STORE.load("outbox.json", []) if x["id"] != item["id"]])
                changed = True
            except Exception as e:  # noqa: BLE001
                diag_exc("scheduled send", e)
                with _lock:
                    box = STORE.load("outbox.json", [])
                    for x in box:
                        if x["id"] == item["id"]:
                            x["sending"] = False
                            x["tries"] = x.get("tries", 0) + 1
                            x["at"] = time.time() + min(600, 15 * 2 ** x["tries"])  # offline: back off
                            if x["tries"] >= 8:
                                x["error"] = str(e)[:300]
                    STORE.save("outbox.json", box)
        with _lock:
            sz = STORE.load("snooze.json", [])
            due_s = [x for x in sz if x["until"] <= now]
        if due_s:
            self.mail_fn(lambda mb: _wake_snoozed(mb, due_s))
            with _lock:
                ids = {x["msgid"] for x in due_s}
                STORE.save("snooze.json", [x for x in STORE.load("snooze.json", []) if x["msgid"] not in ids])
            changed = True
        if changed and self.after_fn:
            self.after_fn()


# --------------------------------------------------------------------------- contacts
_contacts = {"t": 0.0, "items": []}


def contacts(mb: fm.Mailbox, me: str = "", refresh: bool = False) -> list[dict]:
    """Frequent correspondents: recipients of your last 800 sent mails weigh 3, senders of the last 800 inbox mails 1."""
    if not refresh and time.time() - _contacts["t"] < 3600 and _contacts["items"]:
        return _contacts["items"]
    mailweb._refresh(mb)
    score: dict[str, dict] = {}
    me = (me or "").lower()

    def scan(folder_raw: str | None, fields: str, weight: int):
        if not folder_raw:
            return
        n = mb.select(folder_raw)
        if not n:
            return
        lo = max(1, n - 799)
        typ, d = mb.M.fetch(f"{lo}:{n}", f"(BODY.PEEK[HEADER.FIELDS ({fields})])")
        for it in d or []:
            if not isinstance(it, tuple):
                continue
            h = email.message_from_bytes(it[1])
            for k in fields.split():
                for a in mailweb._addr(h.get(k)):
                    e = (a.get("email") or "").lower()
                    if not e or "@" not in e or e == me or re.search(r"no-?reply|ne-?pas-?repondre|notification|mailer-daemon", e):
                        continue
                    c = score.setdefault(e, {"email": e, "name": "", "score": 0})
                    c["score"] += weight
                    if a.get("name") and not c["name"]:
                        c["name"] = a["name"]

    scan(mailweb._special(mb, "sent"), "TO CC", 3)
    scan(mailweb._special(mb, "inbox") or mb.resolve("INBOX"), "FROM", 1)
    items = sorted(score.values(), key=lambda c: -c["score"])[:600]
    _contacts.update(t=time.time(), items=items)
    return items


# --------------------------------------------------------------------------- quick reply (Android notification)
def quick_reply(cfg: dict, password: str, mb: fm.Mailbox, folder: str, uid: str, text: str) -> dict:
    m = mailweb.get_message(mb, folder, uid, mark_seen=True)
    s = get_settings()
    rt = [a["email"] for a in (m["reply_to"] or m["from"]) if a.get("email")]
    subj = m["subject"] if re.match(r"(?i)^re\s*:", m["subject"] or "") else "Re: " + (m["subject"] or "")
    when = m["date"] or ""
    who = (m["from"][0].get("name") or m["from"][0].get("email")) if m["from"] else ""
    quote_txt = "\n".join("> " + ln for ln in (m["text"] or "").splitlines()[:200])
    body_html = "<div>" + htmlmod.escape(text).replace("\n", "<br>") + "</div>"
    if s["signature"]:
        body_html += "<div><br></div><div>-- </div>" + s["signature"]
    body_html += f"<div><br></div><div>{htmlmod.escape(when)}, {htmlmod.escape(who)} :</div><blockquote>{htmlmod.escape(m['text'] or '')[:20000].replace(chr(10), '<br>')}</blockquote>"
    data = {"to": rt, "subject": subj, "from_name": s["from_name"],
            "text": text + ("\n\n-- \n" + mailweb._strip_html(s["signature"]) if s["signature"] else "") + f"\n\n{when}, {who} :\n{quote_txt}",
            "html": f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.55">{body_html}</div>',
            "in_reply_to": m["message_id"], "references": m["references"], "reply_uid": uid, "reply_folder": folder}
    r = mailweb.send(cfg, password, mb, data)
    diag(f"quick reply sent to {', '.join(rt)}")
    return r
