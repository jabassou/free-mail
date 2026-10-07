"""Webmail backend for the freemail web UI: browse folders, read, act on and send mail.

All IMAP work goes through one persistent, lock-protected connection (reconnects on drop).
Message HTML is returned as-is (scripts stripped) and rendered by the browser inside a
sandboxed iframe with a strict CSP; remote images stay blocked unless the user allows them.
"""
from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import email.utils
import html as htmlmod
import imaplib
import os
import re
import smtplib
import ssl
import threading
import time
import urllib.parse
from email.header import decode_header, make_header
from email.message import EmailMessage

import freemail as fm

PAGE = 50
MAX_MSG = 30 * 1024 * 1024
MAX_INLINE = 3 * 1024 * 1024
ROLES = {"\\inbox": "inbox", "\\sent": "sent", "\\drafts": "drafts", "\\trash": "trash", "\\junk": "junk", "\\archive": "archive"}
NAME_ROLES = {"inbox": "inbox", "sent": "sent", "sent items": "sent", "envoyés": "sent", "drafts": "drafts", "brouillons": "drafts",
              "trash": "trash", "corbeille": "trash", "deleted messages": "trash", "junk": "junk", "spam": "junk",
              "courrier indésirable": "junk", "archive": "archive", "archives": "archive"}


class MailError(Exception):
    pass


# --------------------------------------------------------------------------- connection
class Conn:
    """One persistent IMAP connection shared by the webmail endpoints."""

    def __init__(self, cfg_fn, pw_fn):
        self.cfg_fn, self.pw_fn = cfg_fn, pw_fn
        self.mb: fm.Mailbox | None = None
        self.lock = threading.RLock()
        self.last = 0.0

    def _get(self) -> fm.Mailbox:
        if self.mb and time.time() - self.last > 60:
            try:
                self.mb.M.noop()
            except Exception:  # noqa: BLE001
                self.mb = None
        if not self.mb:
            self.mb = fm.Mailbox(self.cfg_fn(), password=self.pw_fn())
        self.last = time.time()
        return self.mb

    def reset(self):
        if self.mb:
            try:
                self.mb.M.logout()
            except Exception:  # noqa: BLE001
                pass
        self.mb = None

    def run(self, fn):
        with self.lock:
            try:
                return fn(self._get())
            except (imaplib.IMAP4.abort, OSError, ssl.SSLError):
                self.reset()
                return fn(self._get())


# --------------------------------------------------------------------------- helpers
def _dec(v) -> str:
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(str(v)))).replace("\r", "").replace("\n", " ").strip()
    except Exception:  # noqa: BLE001
        return str(v)


def _addr(v) -> list[dict]:
    out = []
    for name, addr in email.utils.getaddresses([_dec(v)] if v else []):
        if addr or name:
            out.append({"name": name, "email": addr})
    return out


def _iso(v) -> str:
    try:
        d = email.utils.parsedate_to_datetime(str(v))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _quote_search(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ids(v) -> list[str]:
    """Message-IDs found in a header value, in order, without duplicates."""
    out = []
    for x in re.findall(r"<[^<>\s]+>", str(v or "")):
        if x not in out:
            out.append(x)
    return out


def norm_subject(s: str) -> str:
    s = (s or "").strip()
    while True:
        n = re.sub(r"^\s*(re|fwd?|tr|aw|wg|rv|sv|antw)\s*(\[\d+\])?\s*:\s*", "", s, flags=re.I)
        n = re.sub(r"^\s*\[[^\]]{1,40}\]\s*", "", n) if n == s else n
        if n == s:
            return re.sub(r"\s+", " ", s).lower()
        s = n


def _strip_html(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return htmlmod.unescape(h)


def _snippet(ctype: str, body: bytes, cte: str = "") -> str:
    """Best-effort preview from the first bytes of the body."""
    if not body:
        return ""
    try:
        head = b"Content-Type: " + (ctype or "text/plain").encode() + b"\r\n"
        if cte:
            head += b"Content-Transfer-Encoding: " + cte.encode() + b"\r\n"
        msg = email.message_from_bytes(head + b"\r\n" + body)
        parts = list(msg.walk()) if msg.is_multipart() else [msg]
        best = None
        for p in parts:
            ct = p.get_content_type()
            if ct == "text/plain":
                best = p
                break
            if ct == "text/html" and best is None:
                best = p
        if best is None:
            return ""
        if (best.get("Content-Transfer-Encoding") or "").lower() == "base64":
            raw = best.get_payload(decode=False)
            if isinstance(raw, list):
                return ""
            raw = re.sub(r"[^A-Za-z0-9+/]", "", raw)
            data = base64.b64decode(raw[: len(raw) - len(raw) % 4] or b"")
        else:
            data = best.get_payload(decode=True) or b""
        text = data.decode(best.get_content_charset() or "utf-8", "replace")
        if best.get_content_type() == "text/html":
            text = _strip_html(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:180]
    except Exception:  # noqa: BLE001
        return ""


def _role(f: dict) -> str | None:
    for fl in f["flags"]:
        if fl.lower() in ROLES:
            return ROLES[fl.lower()]
    return NAME_ROLES.get(f["name"].lower())


def _special(mb: fm.Mailbox, role: str) -> str | None:
    for f in mb.folders:
        if _role(f) == role:
            return f["raw"]
    return None


def _refresh(mb: fm.Mailbox):
    typ, d = mb.M.list()
    if typ == "OK":
        mb.folders = fm.parse_list(d)


# --------------------------------------------------------------------------- folders
def folders(mb: fm.Mailbox) -> list[dict]:
    _refresh(mb)
    stats = {s["name"]: s for s in mb.folder_stats()}
    out = []
    for f in mb.folders:
        name = f["name"]
        parts = name.split(f["delim"]) if f["delim"] else [name]
        noselect = "\\noselect" in [x.lower() for x in f["flags"]]
        s = stats.get(name, {})
        out.append({"name": name, "label": parts[-1], "parent": f["delim"].join(parts[:-1]) if len(parts) > 1 else None,
                    "depth": len(parts) - 1, "role": _role(f), "messages": s.get("messages", 0), "uidnext": s.get("uidnext"),
                    "unseen": s.get("unseen", 0), "selectable": not noselect})
    order = {"inbox": 0, "drafts": 1, "sent": 2, "archive": 3, "junk": 4, "trash": 5}
    out.sort(key=lambda x: (order.get(x["role"], 9) if x["depth"] == 0 else 9, x["name"].lower()))
    return out


# --------------------------------------------------------------------------- list
_LIST_ITEMS = "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE CONTENT-TYPE CONTENT-TRANSFER-ENCODING MESSAGE-ID IN-REPLY-TO REFERENCES)] BODY.PEEK[TEXT]<0.2000>)"


def _parse_list_fetch(data) -> list[dict]:
    msgs, cur = [], None
    for it in data:
        if isinstance(it, tuple):
            meta = it[0].decode(errors="replace")
            if re.match(r"^\d+ \(", meta):
                cur = {"meta": meta, "hdr": b"", "text": b""}
                msgs.append(cur)
            elif cur is not None:
                cur["meta"] += " " + meta
            if cur is None:
                continue
            if "HEADER.FIELDS" in meta.upper():
                cur["hdr"] = it[1]
            elif "BODY[TEXT]" in meta.upper():
                cur["text"] = it[1]
        elif isinstance(it, bytes) and cur is not None:
            cur["meta"] += " " + it.decode(errors="replace")
    out = []
    for c in msgs:
        m = c["meta"]
        uid = re.search(r"UID (\d+)", m)
        if not uid:
            continue
        flags = (re.search(r"FLAGS \(([^)]*)\)", m) or [None, ""])[1].lower().split()
        size = re.search(r"RFC822\.SIZE (\d+)", m)
        idate = imaplib.Internaldate2tuple(m.encode())
        h = email.message_from_bytes(c["hdr"] or b"")
        ctype = h.get("Content-Type", "")
        frm = _addr(h.get("From"))
        out.append({
            "uid": uid.group(1),
            "from": frm[0] if frm else {"name": "", "email": ""},
            "to": _addr(h.get("To")),
            "subject": _dec(h.get("Subject")),
            "date": _iso(h.get("Date")) or (dt.datetime.fromtimestamp(time.mktime(idate)).astimezone().isoformat() if idate else ""),
            "size": int(size.group(1)) if size else 0,
            "seen": "\\seen" in flags, "flagged": "\\flagged" in flags, "answered": "\\answered" in flags,
            "attachment": bool(re.search(r"multipart/(mixed|report)", ctype, re.I)),
            "snippet": _snippet(ctype, c["text"], h.get("Content-Transfer-Encoding", "")),
            "msgid": _ids(h.get("Message-ID"))[:1][0] if _ids(h.get("Message-ID")) else "",
            "irt": (_ids(h.get("In-Reply-To")) or [""])[-1],
            "refs": _ids(h.get("References"))[-40:],
        })
    return out


def list_messages(mb: fm.Mailbox, folder: str, page: int = 0, q: str = "", filt: str = "") -> dict:
    raw = mb.resolve(folder)
    total_in_folder = mb.select(raw)
    crit = []
    if filt == "unseen":
        crit.append("UNSEEN")
    elif filt == "flagged":
        crit.append("FLAGGED")
    elif filt == "attachments":
        crit.append('HEADER Content-Type "multipart/mixed"')
    literal = None
    q = (q or "").strip()
    if q:
        if q.isascii():
            qs = _quote_search(q)
            crit.append(f"OR OR FROM {qs} SUBJECT {qs} TO {qs}")
        else:
            literal = q.encode("utf-8")
    crit_s = " ".join(crit) or "ALL"
    if literal is not None:
        mb.M.literal = literal
        crit_s = (" ".join(crit) + " TEXT").strip()
    if "SORT" in mb.caps:
        typ, d = mb.M.uid("SORT", "(REVERSE ARRIVAL)", "UTF-8", crit_s)
        uids = d[0].decode().split() if typ == "OK" and d and d[0] else []
    else:
        args = ("CHARSET", "UTF-8", crit_s) if literal is not None else (crit_s,)
        typ, d = mb.M.uid("SEARCH", *args)
        uids = sorted(d[0].decode().split(), key=int, reverse=True) if typ == "OK" and d and d[0] else []
    page_uids = uids[page * PAGE:(page + 1) * PAGE]
    items = []
    if page_uids:
        typ, d = mb.M.uid("FETCH", ",".join(page_uids), _LIST_ITEMS)
        if typ == "OK":
            by = {m["uid"]: m for m in _parse_list_fetch(d)}
            items = [by[u] for u in page_uids if u in by]
    return {"folder": folder, "total": len(uids), "folder_total": total_in_folder, "page": page,
            "pages": max(1, -(-len(uids) // PAGE)), "items": items}


# --------------------------------------------------------------------------- read
def _fetch_raw(mb: fm.Mailbox, uid: str) -> tuple[bytes, list[str]]:
    typ, d = mb.M.uid("FETCH", uid, "(FLAGS RFC822.SIZE)")
    m = re.search(rb"RFC822\.SIZE (\d+)", d[0] if d and isinstance(d[0], bytes) else b"")
    if m and int(m.group(1)) > MAX_MSG:
        raise MailError("Message trop volumineux pour être affiché ici (> 30 Mo).")
    flags = re.search(rb"FLAGS \(([^)]*)\)", d[0] or b"") if d and isinstance(d[0], bytes) else None
    typ, d = mb.M.uid("FETCH", uid, "(BODY.PEEK[])")
    for it in d or []:
        if isinstance(it, tuple):
            return it[1], (flags.group(1).decode().lower().split() if flags else [])
    raise MailError("Message introuvable (déjà déplacé ou supprimé ?)")


def _clean_html(h: str) -> str:
    h = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", "", h)
    h = re.sub(r"(?is)<(iframe|object|embed|form|base|meta[^>]+http-equiv)[^>]*>", "", h)
    h = re.sub(r'(?i)\son[a-z]+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', "", h)
    h = re.sub(r'(?i)(href|src)\s*=\s*(["\']?)\s*javascript:', r"\1=\2#", h)
    return h


def _linkify(text: str) -> str:
    esc = htmlmod.escape(text.replace("\r\n", "\n").replace("\r", "\n"))
    esc = re.sub(r"(https?://[^\s<>&\"]+)", r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', esc)
    lines = []
    for ln in esc.split("\n"):
        lines.append(f'<span style="color:#6b7280">{ln}</span>' if ln.startswith("&gt;") else ln)
    return '<div style="white-space:pre-wrap;font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;line-height:1.55">' + "\n".join(lines) + "</div>"


def get_message(mb: fm.Mailbox, folder: str, uid: str, mark_seen: bool = True) -> dict:
    raw_f = mb.resolve(folder)
    mb.select(raw_f, readonly=not mark_seen)
    raw, flags = _fetch_raw(mb, uid)
    if mark_seen and "\\seen" not in flags:
        mb.M.uid("STORE", uid, "+FLAGS.SILENT", r"(\Seen)")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    cids, atts = {}, []
    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        cid = (part.get("Content-ID") or "").strip("<> ")
        disp = part.get_content_disposition()
        fname = part.get_filename()
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            payload = b""
        if cid and ctype.startswith("image/") and len(payload) <= MAX_INLINE:
            cids[cid] = f"data:{ctype};base64,{base64.b64encode(payload).decode()}"
        if disp == "attachment" or (fname and disp != "inline") or (fname and not cid):
            atts.append({"part": idx, "filename": _dec(fname) or f"piece-jointe-{idx}", "type": ctype, "size": len(payload),
                         "inline": bool(cid)})
    body_html, body_text = None, None
    hp = msg.get_body(preferencelist=("html",))
    tp = msg.get_body(preferencelist=("plain",))
    try:
        body_html = hp.get_content() if hp is not None else None
    except Exception:  # noqa: BLE001
        body_html = None
    try:
        body_text = tp.get_content() if tp is not None else None
    except Exception:  # noqa: BLE001
        body_text = None
    remote = 0
    if body_html:
        body_html = _clean_html(body_html)
        body_html = re.sub(r"cid:([^\"'\s)>]+)", lambda m: cids.get(m.group(1), "#"), body_html)
        remote = len(re.findall(r"""(?i)(?:src|background)\s*=\s*["']?https?://|url\(\s*["']?https?://""", body_html))
    html_out = body_html or _linkify(body_text or "")
    unsub = str(msg.get("List-Unsubscribe") or "")
    return {
        "uid": uid, "folder": folder,
        "subject": _dec(msg.get("Subject")) or "(sans objet)",
        "from": _addr(msg.get("From")), "to": _addr(msg.get("To")), "cc": _addr(msg.get("Cc")),
        "bcc": _addr(msg.get("Bcc")),
        "reply_to": _addr(msg.get("Reply-To")), "date": _iso(msg.get("Date")),
        "in_reply_to": str(msg.get("In-Reply-To") or ""), "fm_reply": _parse_fm_reply(msg.get("X-FM-Reply")),
        "message_id": str(msg.get("Message-ID") or ""), "references": str(msg.get("References") or ""),
        "html": html_out, "text": body_text or _strip_html(body_html or "")[:20000],
        "is_html": bool(body_html), "remote_images": remote, "attachments": atts,
        "flagged": "\\flagged" in flags, "answered": "\\answered" in flags,
        "unsubscribe": _unsub(msg) or None,
    }


def _unsub(msg) -> dict:
    """List-Unsubscribe (RFC 2369) + List-Unsubscribe-Post one-click (RFC 8058)."""
    raw = re.sub(r"\s+", "", str(msg.get("List-Unsubscribe") or ""))
    http = next(iter(re.findall(r"<(https?://[^>]+)>", raw)), None)
    mailto = next(iter(re.findall(r"<mailto:([^>]+)>", raw, re.I)), None)
    one_click = "one-click" in str(msg.get("List-Unsubscribe-Post") or "").lower()
    if not (http or mailto):
        return {}
    return {"http": http, "mailto": mailto, "one_click": bool(one_click and http)}


def unsubscribe(cfg: dict, password: str, mb: fm.Mailbox, folder: str, uid: str) -> dict:
    """Unsubscribe the way the sender asked for: one-click POST, else an e-mail, else a page to open."""
    mb.select(mb.resolve(folder))
    raw, _ = _fetch_raw(mb, str(int(uid)))
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    u = _unsub(msg)
    if not u:
        raise MailError("Ce mail ne propose pas de désabonnement automatique.")
    sender = (_addr(msg.get("From")) or [{}])[0].get("name") or (_addr(msg.get("From")) or [{}])[0].get("email") or "l'expéditeur"
    if u["one_click"]:
        import requests
        r = requests.post(u["http"], data={"List-Unsubscribe": "One-Click"}, timeout=25, allow_redirects=True,
                          headers={"User-Agent": fm.UA, "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code < 400:
            return {"ok": True, "method": "one-click", "sender": sender}
    if u["mailto"]:
        from urllib.parse import parse_qs, unquote, urlsplit
        parts = urlsplit("mailto:" + u["mailto"])
        q = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
        send(cfg, password, mb, {"to": [unquote(parts.path)], "subject": q.get("subject") or "unsubscribe",
                                 "text": q.get("body") or "unsubscribe", "save_sent": False})
        return {"ok": True, "method": "mailto", "sender": sender}
    return {"ok": False, "open": u["http"], "sender": sender}


def get_attachment(mb: fm.Mailbox, folder: str, uid: str, part: int) -> tuple[bytes, str, str]:
    mb.select(mb.resolve(folder))
    raw, _ = _fetch_raw(mb, uid)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    for idx, p in enumerate(msg.walk()):
        if idx == part and not p.is_multipart():
            return p.get_payload(decode=True) or b"", p.get_content_type(), _dec(p.get_filename()) or f"piece-jointe-{idx}"
    raise MailError("Pièce jointe introuvable")


# --------------------------------------------------------------------------- actions
def _uidset(uids) -> str:
    uids = [str(int(u)) for u in uids]
    if not uids:
        raise MailError("Aucun message sélectionné")
    return ",".join(uids)


def set_flag(mb: fm.Mailbox, folder: str, uids, flag: str, on: bool):
    imap_flag = {"seen": r"\Seen", "flagged": r"\Flagged"}.get(flag)
    if not imap_flag:
        raise MailError("drapeau inconnu")
    mb.select(mb.resolve(folder), readonly=False)
    mb.M.uid("STORE", _uidset(uids), "+FLAGS.SILENT" if on else "-FLAGS.SILENT", f"({imap_flag})")


def mark_all_read(mb: fm.Mailbox, folder: str) -> int:
    mb.select(mb.resolve(folder), readonly=False)
    uids = mb.search("UNSEEN")
    if uids:
        mb.M.uid("STORE", ",".join(uids), "+FLAGS.SILENT", r"(\Seen)")
    return len(uids)


def move(mb: fm.Mailbox, folder: str, uids, dest: str):
    _refresh(mb)
    if not mb.exists(dest):
        raise MailError(f"Dossier « {dest} » introuvable")
    mb.select(mb.resolve(folder), readonly=False)
    mb.move([str(int(u)) for u in uids], mb.resolve(dest))


def delete(mb: fm.Mailbox, folder: str, uids) -> str:
    trash = _special(mb, "trash")
    raw = mb.resolve(folder)
    if trash and raw != trash:
        mb.select(raw, readonly=False)
        mb.move([str(int(u)) for u in uids], trash)
        return "trash"
    mb.select(raw, readonly=False)
    s = _uidset(uids)
    mb.M.uid("STORE", s, "+FLAGS.SILENT", r"(\Deleted)")
    if "UIDPLUS" in mb.caps:
        mb.M.uid("EXPUNGE", s)
    else:
        mb.M.expunge()
    return "deleted"


# --------------------------------------------------------------------------- compose
MAX_ATTACH = 25 * 1024 * 1024


def _special_name(mb: fm.Mailbox, role: str) -> str | None:
    for f in mb.folders:
        if _role(f) == role:
            return f["name"]
    return None


def _parse_fm_reply(v) -> dict | None:
    """X-FM-Reply: <quoted folder> <uid> -- lets a reopened draft flag the original as answered."""
    m = re.match(r"^\s*(\S+)\s+(\d+)\s*$", str(v or ""))
    return {"folder": urllib.parse.unquote(m.group(1)), "uid": m.group(2)} if m else None


def _server_parts(mb: fm.Mailbox, includes: list[dict]) -> list[tuple[bytes, str, str]]:
    """Fetch attachment parts that already live on the server (forwarded mail, previous draft)."""
    by_msg: dict[tuple[str, str], list[int]] = {}
    for inc in includes or []:
        by_msg.setdefault((inc["folder"], str(int(inc["uid"]))), []).append(int(inc["part"]))
    out = []
    for (folder, uid), parts in by_msg.items():
        mb.select(mb.resolve(folder))
        raw, _ = _fetch_raw(mb, uid)
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        walked = list(msg.walk())
        for p in parts:
            if p >= len(walked) or walked[p].is_multipart():
                raise MailError("Pièce jointe d'origine introuvable (message déplacé ou supprimé ?)")
            part = walked[p]
            out.append((part.get_payload(decode=True) or b"", part.get_content_type(),
                        _dec(part.get_filename()) or f"piece-jointe-{p}"))
    return out


def _build(cfg: dict, mb: fm.Mailbox, data: dict, draft: bool = False) -> EmailMessage:
    to, cc, bcc = (data.get(k) or [] for k in ("to", "cc", "bcc"))
    m = EmailMessage()
    m["From"] = email.utils.formataddr((data.get("from_name") or "", cfg["user"]))
    if to:
        m["To"] = ", ".join(to)
    if cc:
        m["Cc"] = ", ".join(cc)
    if bcc and draft:
        m["Bcc"] = ", ".join(bcc)
    m["Subject"] = data.get("subject") or ""
    m["Date"] = email.utils.formatdate(localtime=True)
    m["Message-ID"] = data.get("message_id") or email.utils.make_msgid(domain=cfg["user"].split("@")[-1])
    if data.get("in_reply_to"):
        m["In-Reply-To"] = data["in_reply_to"]
        refs = _ids(data.get("references"))
        if data["in_reply_to"] not in refs:
            refs.append(data["in_reply_to"])
        m["References"] = " ".join(refs[-40:])
    if draft and data.get("reply_uid") and data.get("reply_folder"):
        m["X-FM-Reply"] = f"{urllib.parse.quote(data['reply_folder'], safe='')} {int(data['reply_uid'])}"
    m.set_content(data.get("text") or "")
    if data.get("html"):
        m.add_alternative(data["html"], subtype="html")
    files = []
    for a in data.get("attachments") or []:
        files.append((base64.b64decode(a["data"]), a.get("type") or "application/octet-stream", a.get("name") or "fichier"))
    files += _server_parts(mb, data.get("include") or [])
    total = 0
    for blob, ctype, name in files:
        total += len(blob)
        if total > MAX_ATTACH:
            raise MailError("Pièces jointes trop lourdes (25 Mo max au total).")
        maintype, _, subtype = ctype.partition("/")
        m.add_attachment(blob, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)
    return m


def _expunge_uid(mb: fm.Mailbox, folder_raw: str, uid: str):
    mb.select(folder_raw, readonly=False)
    mb.M.uid("STORE", str(int(uid)), "+FLAGS.SILENT", r"(\Deleted)")
    if "UIDPLUS" in mb.caps:
        mb.M.uid("EXPUNGE", str(int(uid)))
    else:
        mb.M.expunge()


def _att_index(raw: bytes) -> list[dict]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    out = []
    for idx, part in enumerate(msg.walk()):
        if not part.is_multipart() and part.get_content_disposition() == "attachment":
            out.append({"part": idx, "filename": _dec(part.get_filename()) or f"piece-jointe-{idx}",
                        "type": part.get_content_type(), "size": len(part.get_payload(decode=True) or b"")})
    return out


def save_draft(cfg: dict, mb: fm.Mailbox, data: dict) -> dict:
    """Store the draft in the server Drafts folder (APPEND new version, then drop the previous one)."""
    _refresh(mb)
    drafts = _special(mb, "drafts")
    if not drafts:
        raise MailError("Aucun dossier Brouillons sur le serveur.")
    m = _build(cfg, mb, data, draft=True)
    raw = m.as_bytes()
    typ, d = mb.M.append(fm.quote(drafts), r"(\Draft \Seen)", imaplib.Time2Internaldate(time.time()), raw)
    if typ != "OK":
        raise MailError(f"Enregistrement du brouillon refusé : {d}")
    resp = b" ".join(x for x in (d or []) if isinstance(x, bytes)).decode(errors="replace")
    am = re.search(r"APPENDUID \d+ (\d+)", resp)
    new_uid = am.group(1) if am else None
    if not new_uid:
        mb.select(drafts)
        found = mb.search(f'HEADER Message-ID "{m["Message-ID"].strip("<>")}"')
        new_uid = found[-1] if found else None
    old = data.get("draft_uid")
    if old and str(old) != str(new_uid):
        try:
            _expunge_uid(mb, drafts, old)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "uid": new_uid, "folder": _special_name(mb, "drafts"), "message_id": m["Message-ID"],
            "attachments": _att_index(raw), "saved_at": dt.datetime.now().astimezone().isoformat()}


def delete_draft(mb: fm.Mailbox, uid) -> dict:
    _refresh(mb)
    drafts = _special(mb, "drafts")
    if drafts and uid:
        _expunge_uid(mb, drafts, uid)
    return {"ok": True}


def send(cfg: dict, password: str, mb: fm.Mailbox, data: dict) -> dict:
    to, cc, bcc = (data.get(k) or [] for k in ("to", "cc", "bcc"))
    rcpts = [a.strip() for a in to + cc + bcc if a and a.strip()]
    if not rcpts:
        raise MailError("Ajoute au moins un destinataire.")
    for a in rcpts:
        if not re.match(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$", a):
            raise MailError(f"Adresse invalide : {a}")
    _refresh(mb)
    m = _build(cfg, mb, data)
    ctx = ssl.create_default_context()
    if os.environ.get("FREEMAIL_INSECURE_TLS"):
        ctx = ssl._create_unverified_context()
    host, port = cfg.get("smtp_host", "smtp.free.fr"), int(cfg.get("smtp_port", 465))
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
        s.login(cfg["user"], password)
        s.send_message(m, to_addrs=rcpts)
    saved = False
    sent = _special(mb, "sent")
    if sent and data.get("save_sent", True):
        try:
            mb.M.append(fm.quote(sent), r"(\Seen)", imaplib.Time2Internaldate(time.time()), m.as_bytes())
            saved = True
        except Exception:  # noqa: BLE001
            pass
    if data.get("reply_uid") and data.get("reply_folder"):
        try:
            mb.select(mb.resolve(data["reply_folder"]), readonly=False)
            mb.M.uid("STORE", str(int(data["reply_uid"])), "+FLAGS.SILENT", r"(\Answered)")
        except Exception:  # noqa: BLE001
            pass
    if data.get("draft_uid"):
        try:
            delete_draft(mb, data["draft_uid"])
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "saved_to_sent": saved, "recipients": len(rcpts)}


# --------------------------------------------------------------------------- conversations
def _or(crits: list[str]) -> str:
    if len(crits) == 1:
        return crits[0]
    return f"OR {crits[0]} {_or(crits[1:])}"


def _safe_id(x: str) -> str:
    return '"' + re.sub(r'["\\\r\n]', "", x) + '"'


def thread(mb: fm.Mailbox, folder: str, uid: str) -> dict:
    """All messages of the conversation `uid` belongs to, across this folder, Inbox and Sent."""
    _refresh(mb)
    raw_f = mb.resolve(folder)
    mb.select(raw_f)
    typ, d = mb.M.uid("FETCH", str(int(uid)), _LIST_ITEMS)
    base = _parse_list_fetch(d) if typ == "OK" else []
    if not base:
        raise MailError("Message introuvable")
    me = base[0]
    chain = [x for x in me["refs"] + [me["irt"], me["msgid"]] if x]
    chain = list(dict.fromkeys(chain))[-20:]
    crits = []
    for x in chain:
        crits.append(f"HEADER Message-ID {_safe_id(x)}")
    for x in chain[:1] + chain[-1:]:
        crits.append(f"HEADER References {_safe_id(x)}")
        crits.append(f"HEADER In-Reply-To {_safe_id(x)}")
    crits = list(dict.fromkeys(crits))
    places = [raw_f] + [r for r in (_special(mb, "inbox") or mb.resolve("INBOX"), _special(mb, "sent")) if r and r != raw_f]
    found, seen_ids = [], set()
    subj = norm_subject(me["subject"])
    for place in places:
        try:
            mb.select(place)
            uids = mb.search(_or(crits)) if crits else []
            if len(subj) >= 4 and subj.isascii():
                cand = mb.search(f"SUBJECT {_quote_search(subj[:60])}")[-60:]
                uids = list(dict.fromkeys(uids + cand))
            uids = uids[-100:]
            if not uids:
                continue
            typ, d = mb.M.uid("FETCH", ",".join(uids), _LIST_ITEMS)
            items = _parse_list_fetch(d) if typ == "OK" else []
        except Exception:  # noqa: BLE001
            continue
        ids_all = set(chain)
        for it in items:
            linked = it["msgid"] in ids_all or it["irt"] in ids_all or bool(set(it["refs"]) & ids_all)
            same_subj = norm_subject(it["subject"]) == subj and bool(it["irt"] or it["refs"]) and bool(me["irt"] or me["refs"])
            if not (linked or same_subj):
                continue
            key = it["msgid"] or f"{place}:{it['uid']}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            it["folder"] = next((f["name"] for f in mb.folders if f["raw"] == place), fm.utf7_decode(place))
            found.append(it)
    if not any(x["uid"] == me["uid"] and x["folder"] == folder for x in found):
        me["folder"] = folder
        found.append(me)
    found.sort(key=lambda x: x["date"] or "")
    for x in found:
        x.pop("refs", None)
    return {"subject": me["subject"], "items": found[-50:]}
