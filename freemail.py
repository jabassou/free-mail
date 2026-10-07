#!/usr/bin/env python3
"""freemail - report / clean / server-side filters for a Free (Zimbra) mailbox.

Commands:
  auth [--check]                 store password in OS keyring / test IMAP login
  folders                        list folders with message counts
  report [--folder F] [--since]  read-only stats (senders, domains, sizes, newsletters)
  clean -r rules.yaml [--apply]  rule-based cleanup, dry-run by default
  filters test|pull|plan|push|restore   manage Zimbra server-side filters (SOAP)
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import email
import email.utils
import getpass
import imaplib
import json
import os
import re
import sys
import time
from collections import defaultdict
from email.header import decode_header, make_header
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
BACKUPS = ROOT / "backups"
KEYRING_SERVICE = "freemail"
CHUNK = 300
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

imaplib.Commands.setdefault("MOVE", ("SELECTED",))
imaplib._MAXLINE = 10_000_000


# --------------------------------------------------------------------------- config / auth
def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Config not found: {p} (copy config.example.yaml to config.yaml)")
    cfg = yaml.safe_load(p.read_text()) or {}
    if not cfg.get("user"):
        sys.exit("config: 'user' is required (full address, e.g. john@free.fr)")
    cfg.setdefault("imap_host", "imap.free.fr")
    cfg.setdefault("imap_port", 993)
    cfg.setdefault("zimbra_url", "https://zimbra.free.fr")
    return cfg


def get_password(user: str) -> str:
    pw = os.environ.get("FREEMAIL_PASSWORD")
    if pw:
        return pw
    try:
        import keyring
        pw = keyring.get_password(KEYRING_SERVICE, user)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[warn] keyring unavailable: {e}", file=sys.stderr)
    if not pw:
        pf = Path(os.environ.get("FREEMAIL_PASSWORD_FILE") or Path.home() / ".config" / "freemail" / "password")
        if pf.is_file():
            if pf.stat().st_mode & 0o077:
                print(f"[warn] {pf} is readable by others: chmod 600 it", file=sys.stderr)
            pw = pf.read_text().strip() or None
    if not pw:
        sys.exit("No password found: run `./freemail.py auth`, export FREEMAIL_PASSWORD or create ~/.config/freemail/password (chmod 600)")
    return pw


# --------------------------------------------------------------------------- IMAP helpers
def utf7_encode(s: str) -> str:
    """Encode a folder name to IMAP modified UTF-7 (RFC 3501)."""
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf.clear()

    for c in s:
        if 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            buf.append(c)
    flush()
    return "".join(out)


def utf7_decode(s: str) -> str:
    def rep(m):
        x = m.group(1)
        if not x:
            return "&"
        x = x.replace(",", "/")
        x += "=" * (-len(x) % 4)
        return base64.b64decode(x).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", rep, s)


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(d: dt.date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def dec(v) -> str:
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(str(v))))
    except Exception:  # noqa: BLE001
        return str(v)


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')


def parse_list(data) -> list[dict]:
    res = []
    for raw in data:
        if raw is None:
            continue
        if isinstance(raw, tuple):
            line = re.sub(r"\{\d+\}$", "", raw[0].decode()) + quote(raw[1].decode())
        else:
            line = raw.decode()
        m = LIST_RE.match(line.strip())
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        delim = m.group("delim").strip('"') if m.group("delim") != "NIL" else "/"
        res.append({"raw": name, "name": utf7_decode(name), "flags": m.group("flags").split(), "delim": delim})
    return res


def parse_fetch(data) -> list[dict]:
    items, cur = [], None
    for it in data:
        if isinstance(it, tuple):
            cur = {"meta": it[0].decode(errors="replace"), "hdr": it[1]}
            items.append(cur)
        elif isinstance(it, bytes) and cur is not None:
            cur["meta"] += " " + it.decode(errors="replace")
    rows = []
    for c in items:
        m = c["meta"]
        uid = re.search(r"UID (\d+)", m)
        if not uid:
            continue
        size = re.search(r"RFC822\.SIZE (\d+)", m)
        idate = imaplib.Internaldate2tuple(m.encode())
        msg = email.message_from_bytes(c["hdr"] or b"")
        frm = dec(msg.get("From"))
        name, addr = email.utils.parseaddr(frm)
        addr = addr.lower()
        rows.append({
            "uid": uid.group(1),
            "size": int(size.group(1)) if size else 0,
            "date": dt.datetime.fromtimestamp(time.mktime(idate)) if idate else None,
            "from_name": name,
            "from": addr,
            "domain": addr.split("@", 1)[1] if "@" in addr else "",
            "subject": dec(msg.get("Subject")).replace("\n", " ").strip(),
            "unsub": dec(msg.get("List-Unsubscribe")).strip(),
        })
    return rows


class Mailbox:
    def __init__(self, cfg: dict, password: str | None = None):
        import ssl
        ctx = ssl.create_default_context()  # verify the server certificate (imaplib's default does not)
        if os.environ.get("FREEMAIL_INSECURE_TLS"):  # tests against a self-signed server only
            ctx = ssl._create_unverified_context()
        self.M = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), ssl_context=ctx, timeout=60)
        self.password = password
        self.M.login(cfg["user"], password or get_password(cfg["user"]))
        typ, d = self.M.capability()
        self.caps = set(d[0].decode().upper().split()) if typ == "OK" else set()
        typ, d = self.M.list()
        self.folders = parse_list(d)
        self.delim = self.folders[0]["delim"] if self.folders else "/"
        self._warned = False

    def close(self):
        try:
            self.M.logout()
        except Exception:  # noqa: BLE001
            pass

    def selectable(self) -> list[dict]:
        return [f for f in self.folders if "\\noselect" not in [x.lower() for x in f["flags"]]]

    def resolve(self, name: str) -> str:
        for f in self.folders:
            if f["name"] == name or f["raw"] == name or f["name"].lower() == name.lower():
                return f["raw"]
        return utf7_encode(name)

    def exists(self, name: str) -> bool:
        raw = self.resolve(name)
        return any(f["raw"] == raw for f in self.folders)

    def special(self, flag: str, fallbacks: list[str]) -> str | None:
        for f in self.folders:
            if flag.lower() in [x.lower() for x in f["flags"]]:
                return f["raw"]
        for fb in fallbacks:
            for f in self.folders:
                if f["name"].lower() == fb.lower():
                    return f["raw"]
        return None

    def select(self, raw: str, readonly: bool = True) -> int:
        typ, d = self.M.select(quote(raw), readonly=readonly)
        if typ != "OK":
            raise RuntimeError(f"SELECT {utf7_decode(raw)} failed: {d}")
        return int(d[0] or 0)

    def search(self, criteria: str) -> list[str]:
        typ, d = self.M.uid("SEARCH", criteria)
        if typ != "OK":
            raise RuntimeError(f"SEARCH {criteria} failed: {d}")
        return d[0].decode().split() if d and d[0] else []

    def fetch(self, uids: list[str], on_chunk=None) -> list[dict]:
        rows, size = [], 200
        for i in range(0, len(uids), size):
            if on_chunk:
                on_chunk(i, len(uids))
            chunk = ",".join(uids[i:i + size])
            typ, d = self.M.uid(
                "FETCH", chunk,
                "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE)])",
            )
            if typ == "OK":
                rows.extend(parse_fetch(d))
        if on_chunk:
            on_chunk(len(uids), len(uids))
        return rows

    def create(self, name: str) -> str:
        raw = utf7_encode(name)
        typ, d = self.M.create(quote(raw))
        if typ != "OK":
            raise RuntimeError(f"CREATE {name} failed: {d}")
        self.folders.append({"raw": raw, "name": name, "flags": [], "delim": self.delim})
        return raw

    def move(self, uids: list[str], dest_raw: str):
        for i in range(0, len(uids), CHUNK):
            s = ",".join(uids[i:i + CHUNK])
            if "MOVE" in self.caps:
                typ, d = self.M.uid("MOVE", s, quote(dest_raw))
                if typ != "OK":
                    raise RuntimeError(f"MOVE failed: {d}")
                continue
            typ, d = self.M.uid("COPY", s, quote(dest_raw))
            if typ != "OK":
                raise RuntimeError(f"COPY failed: {d}")
            self.M.uid("STORE", s, "+FLAGS.SILENT", r"(\Deleted)")
            if "UIDPLUS" in self.caps:
                self.M.uid("EXPUNGE", s)
            elif not self._warned:
                print("[warn] server lacks MOVE/UIDPLUS: source copies left flagged \\Deleted (not expunged)")
                self._warned = True

    def quota(self) -> dict | None:
        """Returns {'used': KB, 'limit': KB} or None."""
        if "QUOTA" not in self.caps:
            return None
        try:
            typ, d = self.M.getquotaroot("INBOX")
            m = re.search(r"STORAGE (\d+) (\d+)", " ".join(x.decode() for x in d[1] if isinstance(x, bytes)))
            return {"used": int(m.group(1)), "limit": int(m.group(2))} if m else None
        except Exception:  # noqa: BLE001
            return None

    def folder_stats(self) -> list[dict]:
        """Counts for every folder; one round-trip with LIST-STATUS (RFC 5819) when supported."""
        if "LIST-STATUS" in self.caps:
            try:
                typ, dat = self.M._simple_command("LIST", '""', '"*"', "RETURN", "(STATUS (MESSAGES UNSEEN UIDNEXT))")
                self.M.untagged_responses.pop("LIST", None)
                typ, st = self.M._untagged_response(typ, dat, "STATUS")
                counts, pending = {}, None
                for item in st or []:
                    if isinstance(item, tuple):  # literal mailbox name
                        pending = item[1].decode()
                        continue
                    if item is None:
                        continue
                    line = item.decode()
                    m = re.match(r'^(?:"((?:[^"\\]|\\.)*)"|(\S+))?\s*\((.*)\)\s*$', line)
                    if not m:
                        continue
                    name = pending if pending is not None else (m.group(1).replace('\\"', '"') if m.group(1) is not None else m.group(2))
                    pending = None
                    kv = dict(re.findall(r"(MESSAGES|UNSEEN|UIDNEXT) (\d+)", m.group(3)))
                    counts[name] = (int(kv.get("MESSAGES", 0)), int(kv.get("UNSEEN", 0)), int(kv["UIDNEXT"]) if "UIDNEXT" in kv else None)
                if counts:
                    return [{"name": f["name"], "messages": counts.get(f["raw"], (0, 0, None))[0],
                             "unseen": counts.get(f["raw"], (0, 0, None))[1], "uidnext": counts.get(f["raw"], (0, 0, None))[2],
                             "flags": f["flags"]} for f in self.selectable()]
            except Exception:  # noqa: BLE001 - fall back to one STATUS per folder
                pass
        res = []
        for f in self.selectable():
            typ, d = self.M.status(quote(f["raw"]), "(MESSAGES UNSEEN UIDNEXT)")
            line = d[0].decode() if typ == "OK" and d and d[0] else ""
            kv = dict(re.findall(r"(MESSAGES|UNSEEN|UIDNEXT) (\d+)", line))
            res.append({"name": f["name"], "messages": int(kv.get("MESSAGES", 0)), "unseen": int(kv.get("UNSEEN", 0)),
                        "uidnext": int(kv["UIDNEXT"]) if "UIDNEXT" in kv else None, "flags": f["flags"]})
        return res

    def empty(self, raw: str) -> int:
        """PERMANENTLY delete every message of a folder. Returns count."""
        n = self.select(raw, readonly=False)
        if n:
            self.M.store("1:*", "+FLAGS.SILENT", r"(\Deleted)")
            self.M.expunge()
        return n

    def store(self, uids: list[str], flag: str):
        for i in range(0, len(uids), CHUNK):
            self.M.uid("STORE", ",".join(uids[i:i + CHUNK]), "+FLAGS.SILENT", f"({flag})")


# --------------------------------------------------------------------------- commands: auth / folders
def cmd_auth(cfg, args):
    if not args.check:
        import keyring
        pw = getpass.getpass(f"Free password for {cfg['user']}: ")
        keyring.set_password(KEYRING_SERVICE, cfg["user"], pw)
        print(f"Stored in keyring (service={KEYRING_SERVICE}, user={cfg['user']})")
    mb = Mailbox(cfg)
    print(f"IMAP login OK - {len(mb.folders)} folders, caps: {' '.join(sorted(mb.caps))}")
    mb.close()


def cmd_folders(cfg, args):
    mb = Mailbox(cfg)
    for f in mb.folders:
        cnt = ""
        if f in mb.selectable():
            typ, d = mb.M.status(quote(f["raw"]), "(MESSAGES UNSEEN)")
            if typ == "OK":
                m = re.search(r"MESSAGES (\d+).*UNSEEN (\d+)", d[0].decode())
                cnt = f"{m.group(1):>7} msgs  {m.group(2):>6} unseen" if m else ""
        print(f"{f['name']:<45} {cnt}  {' '.join(f['flags'])}")
    mb.close()


def cmd_prune(cfg, args):
    """Delete empty, non-special folders (leaf first). Dry-run by default."""
    mb = Mailbox(cfg)
    keep = {"inbox", "sent", "drafts", "trash", "junk", "contacts", "emailed contacts", "chats"}
    special = {"\\trash", "\\sent", "\\drafts", "\\junk", "\\all", "\\archive"}
    names = {f["name"] for f in mb.folders}
    cands = []
    for f in sorted(mb.folders, key=lambda x: x["name"].count(x["delim"]), reverse=True):
        if f["name"].lower() in keep or special & {x.lower() for x in f["flags"]}:
            continue
        if args.exclude and f["name"] in args.exclude:
            continue
        typ, d = mb.M.status(quote(f["raw"]), "(MESSAGES)")
        n = int(re.search(r"MESSAGES (\d+)", d[0].decode()).group(1)) if typ == "OK" else -1
        children = [x for x in names if x.startswith(f["name"] + f["delim"])]
        if n == 0 and all(c in [y["name"] for y in cands] for c in children):
            cands.append(f)
    for f in cands:
        print(f"  {'DELETE' if args.apply else 'would delete'}: {f['name']}")
        if args.apply:
            typ, d = mb.M.delete(quote(f["raw"]))
            if typ != "OK":
                print(f"    [fail] {d}")
    print(f"{len(cands)} empty folder(s)" + ("" if args.apply else " - dry-run, use --apply"))
    mb.close()


def cmd_login(cfg, args):
    if args.show:
        cfg = {**cfg, "_headless": False}
    try:
        _, n = webmail_login(cfg, method=args.method)
    except LoginError as e:
        sys.exit(str(e))
    print(f"OK : session webmail ouverte, {n} filtres serveur. Les commandes `filters` se connectent désormais seules.")


def cmd_mkdir(cfg, args):
    mb = Mailbox(cfg)
    for name in args.names:
        if mb.exists(name):
            print(f"  exists: {name}")
        else:
            mb.create(name)
            print(f"  created: {name}")
    mb.close()


# --------------------------------------------------------------------------- command: report
def _write_csv(path: Path, header: list[str], rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f}"


def cmd_report(cfg, args, progress=print, step=None):
    mb = Mailbox(cfg)
    folders = [f for f in mb.selectable() if not args.folder or f["name"] in args.folder]
    crit = f"SINCE {imap_date(dt.date.today() - dt.timedelta(days=args.since))}" if args.since else "ALL"
    senders = defaultdict(lambda: {"count": 0, "size": 0, "name": "", "unsub": "", "first": None, "last": None, "folders": set()})
    domains = defaultdict(lambda: {"count": 0, "size": 0, "newsletter": 0})
    per_folder, all_rows = [], []
    ages = defaultdict(lambda: [0, 0])
    now = dt.datetime.now()

    q = mb.quota()
    quota = f"{q['used'] / 1_048_576:.2f} GB / {q['limit'] / 1_048_576:.2f} GB ({q['used'] * 100 / q['limit']:.0f}%)" if q else ""
    if quota:
        progress(f"Quota: {quota}")
    years = defaultdict(int)
    inbox_senders = defaultdict(int)
    nmsg = 0
    for i, f in enumerate(folders):
        if step:
            step(i, len(folders), f["name"], nmsg)
        try:
            mb.select(f["raw"])
            uids = mb.search(crit)
        except RuntimeError as e:
            progress(f"[skip] {f['name']}: {e}")
            continue
        progress(f"  {f['name']}: {len(uids)} messages")
        rows = mb.fetch(uids, on_chunk=(lambda d, t, i=i, f=f: step(i + (d / t if t else 1), len(folders), f["name"], nmsg + d)) if step else None)
        nmsg += len(rows)
        per_folder.append((f["name"], len(rows), sum(r["size"] for r in rows)))
        for r in rows:
            r["folder"] = f["name"]
            all_rows.append(r)
            s = senders[r["from"]]
            s["count"] += 1
            s["size"] += r["size"]
            s["name"] = s["name"] or r["from_name"]
            s["unsub"] = s["unsub"] or r["unsub"]
            s["folders"].add(f["name"])
            if f["name"] == "INBOX":
                inbox_senders[r["from"]] += 1
            if r["date"]:
                s["first"] = min(filter(None, [s["first"], r["date"]]))
                s["last"] = max(filter(None, [s["last"], r["date"]]))
                days = (now - r["date"]).days
                b = "<30d" if days < 30 else "<1y" if days < 365 else "<3y" if days < 1095 else ">=3y"
                ages[b][0] += 1
                ages[b][1] += r["size"]
                years[r["date"].year] += 1
            d = domains[r["domain"]]
            d["count"] += 1
            d["size"] += r["size"]
            d["newsletter"] += 1 if r["unsub"] else 0
    mb.close()
    if step:
        step(len(folders), len(folders), "écriture du rapport", nmsg)

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = OUT / f"report-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    fmt = lambda x: x.strftime("%Y-%m-%d") if x else ""  # noqa: E731
    srt = sorted(senders.items(), key=lambda kv: kv[1]["count"], reverse=True)
    _write_csv(out / "senders.csv", ["from", "name", "count", "size_mb", "newsletter", "first", "last", "folders", "unsubscribe"],
               [(k, v["name"], v["count"], _mb(v["size"]), bool(v["unsub"]), fmt(v["first"]), fmt(v["last"]),
                 "|".join(sorted(v["folders"])), v["unsub"]) for k, v in srt])
    _write_csv(out / "domains.csv", ["domain", "count", "size_mb", "newsletter_msgs"],
               [(k, v["count"], _mb(v["size"]), v["newsletter"]) for k, v in sorted(domains.items(), key=lambda kv: kv[1]["count"], reverse=True)])
    _write_csv(out / "folders.csv", ["folder", "count", "size_mb"], [(n, c, _mb(s)) for n, c, s in per_folder])
    big = sorted(all_rows, key=lambda r: r["size"], reverse=True)[:200]
    _write_csv(out / "biggest.csv", ["folder", "uid", "size_mb", "date", "from", "subject"],
               [(r["folder"], r["uid"], _mb(r["size"]), fmt(r["date"]), r["from"], r["subject"]) for r in big])

    total = sum(r["size"] for r in all_rows)
    news = [(k, v) for k, v in srt if v["unsub"]]
    L = [f"# Mailbox report - {cfg['user']} - {ts}", "",
         f"- Messages: **{len(all_rows)}** - Size: **{_mb(total)} MB** - Distinct senders: {len(senders)}",
         f"- Quota: {quota or 'n/a'}",
         f"- Newsletter-like messages (List-Unsubscribe): **{sum(v['count'] for _, v in news)}** from {len(news)} senders", "",
         "## Folders", "", "| Folder | Msgs | MB |", "|---|---:|---:|"]
    L += [f"| {n} | {c} | {_mb(s)} |" for n, c, s in sorted(per_folder, key=lambda x: x[1], reverse=True)]
    L += ["", "## Age", "", "| Bucket | Msgs | MB |", "|---|---:|---:|"]
    L += [f"| {b} | {ages[b][0]} | {_mb(ages[b][1])} |" for b in ["<30d", "<1y", "<3y", ">=3y"]]
    L += ["", "## Top senders (count)", "", "| From | Msgs | MB | Newsletter |", "|---|---:|---:|:---:|"]
    L += [f"| {k} | {v['count']} | {_mb(v['size'])} | {'x' if v['unsub'] else ''} |" for k, v in srt[:30]]
    L += ["", "## Top newsletters", "", "| From | Msgs | Last |", "|---|---:|---|"]
    L += [f"| {k} | {v['count']} | {fmt(v['last'])} |" for k, v in news[:30]]
    L += ["", "## Biggest messages", "", "| MB | Date | From | Subject |", "|---:|---|---|---|"]
    L += [f"| {_mb(r['size'])} | {fmt(r['date'])} | {r['from']} | {r['subject'][:70].replace('|', '/')} |" for r in big[:20]]
    (out / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    # rule suggestions: newsletter domains with volume -> review before use
    sugg = {"rules": []}
    for dom, v in sorted(domains.items(), key=lambda kv: kv[1]["newsletter"], reverse=True)[:20]:
        if dom and v["newsletter"] >= args.min_newsletter:
            sugg["rules"].append({"name": f"newsletter {dom}", "folders": ["INBOX"],
                                  "match": {"from_domain": [dom], "newsletter": True, "older_than_days": 30},
                                  "action": {"move": "Newsletters"}})
    (out / "rules.suggested.yaml").write_text(
        "# Auto-generated from report - REVIEW before use with `clean -r`\n" + yaml.safe_dump(sugg, sort_keys=False, allow_unicode=True))
    stats = {
        "user": cfg["user"], "generated": dt.datetime.now().isoformat(timespec="seconds"), "quota": q,
        "total": {"messages": len(all_rows), "size": total, "senders": len(senders),
                  "newsletters": sum(v["count"] for _, v in news)},
        "folders": [{"name": n, "count": c, "size": sz} for n, c, sz in per_folder],
        "ages": {b: {"count": ages[b][0], "size": ages[b][1]} for b in ["<30d", "<1y", "<3y", ">=3y"]},
        "years": {str(y): c for y, c in sorted(years.items())},
        "top_senders": [{"from": k, "name": v["name"], "count": v["count"], "size": v["size"], "newsletter": bool(v["unsub"])}
                        for k, v in srt[:30]],
        "inbox_senders": [{"from": k, "count": c} for k, c in sorted(inbox_senders.items(), key=lambda kv: -kv[1])[:30]],
        "top_domains": [{"domain": k, "count": v["count"], "size": v["size"], "newsletter": v["newsletter"]}
                        for k, v in sorted(domains.items(), key=lambda kv: kv[1]["count"], reverse=True)[:30]],
        "newsletters": [{"from": k, "count": v["count"], "last": fmt(v["last"]), "unsubscribe": v["unsub"]} for k, v in news[:30]],
        "biggest": [{"folder": r["folder"], "size": r["size"], "date": fmt(r["date"]), "from": r["from"], "subject": r["subject"]}
                    for r in big[:25]],
    }
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    progress(f"{len(all_rows)} msgs, {_mb(total)} MB -> {out.relative_to(ROOT)}/")
    return out, stats


# --------------------------------------------------------------------------- command: clean
def _lst(v):
    return v if isinstance(v, list) else [v]


def _qs(v: str) -> str:
    if not str(v).isascii():
        raise ValueError(f"non-ASCII value {v!r} not supported server-side: use subject_regex / from_regex")
    return quote(str(v))


def _or(terms: list[str]) -> str:
    return terms[0] if len(terms) == 1 else f"(OR {terms[0]} {_or(terms[1:])})"


def build_criteria(match: dict, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    c = []
    for key, imap in (("from", "FROM"), ("to", "TO"), ("subject", "SUBJECT")):
        if key in match:
            c.append(_or([f"{imap} {_qs(x)}" for x in _lst(match[key])]))
    if "from_domain" in match:
        c.append(_or([f"FROM {_qs('@' + x.lstrip('@'))}" for x in _lst(match['from_domain'])]))
    if "older_than_days" in match:
        c.append(f"BEFORE {imap_date(today - dt.timedelta(days=int(match['older_than_days'])))}")
    if "newer_than_days" in match:
        c.append(f"SINCE {imap_date(today - dt.timedelta(days=int(match['newer_than_days'])))}")
    if "larger_than_kb" in match:
        c.append(f"LARGER {int(match['larger_than_kb']) * 1024}")
    if "unseen" in match:
        c.append("UNSEEN" if match["unseen"] else "SEEN")
    if "flagged" in match:
        c.append("FLAGGED" if match["flagged"] else "UNFLAGGED")
    return " ".join(c) or "ALL"


def client_filter(match: dict, rows: list[dict]) -> list[dict]:
    if "newsletter" in match:
        rows = [r for r in rows if bool(r["unsub"]) == bool(match["newsletter"])]
    if "subject_regex" in match:
        rx = re.compile(match["subject_regex"], re.I)
        rows = [r for r in rows if rx.search(r["subject"])]
    if "from_regex" in match:
        rx = re.compile(match["from_regex"], re.I)
        rows = [r for r in rows if rx.search(r["from"]) or rx.search(r["from_name"])]
    return rows


def cmd_clean(cfg, args):
    spec = yaml.safe_load(Path(args.rules).read_text()) or {}
    run_rules(cfg, spec, args)


def plan_rules(mb: "Mailbox", spec: dict, only=None, log=print, progress=None):
    """Evaluate rules against the mailbox. Returns (plan, trash_raw); plan items are dicts."""
    import fnmatch
    rules = spec.get("rules", [])
    if only:
        rules = [r for r in rules if any(fnmatch.fnmatch(r.get("name", ""), pat) for pat in only)]
    trash = mb.special("\\Trash", ["Trash", "Corbeille", "Deleted Messages"]) if spec.get("trash_folder", "auto") == "auto" \
        else mb.resolve(spec["trash_folder"])
    plan = []
    taken = defaultdict(set)  # folder_raw -> uids already claimed (first matching rule wins)
    pairs = [(r, f) for r in rules for f in r.get("folders", ["INBOX"])]
    prog = progress or (lambda frac, label, detail="": None)
    for idx, (rule, fname) in enumerate(pairs):
        match = rule.get("match", {})
        crit = rule.get("_criteria") or build_criteria(match)
        name = rule.get("name", "?")
        if True:
            raw = mb.resolve(fname)
            prog(idx / len(pairs), f"{name} · {fname}", "recherche des mails…")
            try:
                mb.select(raw)
                uids = mb.search(crit)
            except RuntimeError as e:
                log(f"[skip] {name} / {fname}: {e}")
                continue
            uids = [u for u in uids if u not in taken[raw]]
            log(f"{name} [{fname}] : {len(uids)} mail(s) trouvé(s), lecture des en-têtes…" if uids else f"{name} [{fname}] : aucun mail")
            on_chunk = (lambda d, t, idx=idx, name=name, fname=fname:
                        prog((idx + (d / t if t else 1)) / len(pairs), f"{name} · {fname}", f"lecture des en-têtes {d}/{t}"))
            rows = client_filter(match, mb.fetch(uids, on_chunk=on_chunk)) if uids else []
            taken[raw].update(r["uid"] for r in rows)
            plan.append({"rule": rule, "raw": raw, "folder": fname, "rows": rows, "criteria": crit,
                         "action": rule.get("action", "trash")})
    prog(1, "Simulation terminée", f"{sum(len(p['rows']) for p in plan)} mail(s) concerné(s)")
    return plan, trash


def apply_plan(mb: "Mailbox", plan: list[dict], trash: str | None, log=print, progress=None) -> Path:
    prog = progress or (lambda frac, label, detail="": None)
    total = sum(len(p["rows"]) for p in plan) or 1
    done = 0
    OUT.mkdir(exist_ok=True)
    path = OUT / f"clean-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rule", "folder", "uid", "date", "from", "subject", "action"])
        for p in plan:
            rows, act, name = p["rows"], p["action"], p["rule"].get("name")
            if not rows:
                continue
            all_uids = [r["uid"] for r in rows]
            mb.select(p["raw"], readonly=False)
            dest_raw = None
            if act == "trash":
                if not trash:
                    raise RuntimeError("Trash folder not found: set trash_folder in rules file")
                dest_raw = trash
            elif isinstance(act, dict) and "move" in act:
                dest = act["move"]
                dest_raw = mb.resolve(dest) if mb.exists(dest) else mb.create(dest)
            elif act not in ("mark_read", "flag"):
                raise RuntimeError(f"unknown action {act!r} in rule {name}")
            for i in range(0, len(all_uids), 100):
                uids = all_uids[i:i + 100]
                prog(done / total, f"{name} · {p['folder']}", f"{done + len(uids)}/{total} mails traités")
                if dest_raw:
                    mb.move(uids, dest_raw)
                else:
                    mb.store(uids, r"\Seen" if act == "mark_read" else r"\Flagged")
                done += len(uids)
            for r in rows:
                w.writerow([name, p["folder"], r["uid"], r["date"], r["from"], r["subject"], json.dumps(act)])
            log(f"done: {name} [{p['folder']}] {len(all_uids)} msgs")
    prog(1, "Terminé", f"{done} mails traités")
    return path


def run_rules(cfg, spec, args):
    mb = Mailbox(cfg)
    plan, trash = plan_rules(mb, spec, args.only)
    if not plan:
        mb.close()
        sys.exit("no rules")
    total = 0
    for p in plan:
        rows = p["rows"]
        print(f"\n== {p['rule'].get('name', '?')} [{p['folder']}] -> {p['action']}: {len(rows)} msgs, "
              f"{_mb(sum(r['size'] for r in rows))} MB  (IMAP: {p['criteria']})")
        for r in rows[: args.samples]:
            print(f"   {r['date']:%Y-%m-%d} {r['from'][:35]:<35} {r['subject'][:70]}" if r["date"] else f"   {r['from']} {r['subject'][:70]}")
        if len(rows) > args.samples:
            print(f"   ... +{len(rows) - args.samples}")
        total += len(rows)
    if not args.apply:
        print(f"\nDRY-RUN: {total} messages would be affected. Re-run with --apply to execute.")
        mb.close()
        return
    if total == 0:
        mb.close()
        return
    if not args.yes and input(f"\nApply actions on {total} messages? [y/N] ").strip().lower() != "y":
        sys.exit("aborted")
    path = apply_plan(mb, plan, trash)
    mb.close()
    print(f"log: {path.relative_to(ROOT)}")


# --------------------------------------------------------------------------- command: filters (Zimbra SOAP/JSON)
class ZimbraFault(Exception):
    pass


class Zimbra:
    def __init__(self, cfg: dict, cookie: str | None = None):
        import requests
        self.url = cfg["zimbra_url"].rstrip("/") + "/service/soap"
        self.s = requests.Session()
        base = cfg["zimbra_url"].rstrip("/")
        # Free's front-end only routes /service/soap/<RequestName> with the web client's content type
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) freemail/1.0",
            "Content-Type": "application/soap+xml; charset=UTF-8",
            "Accept": "*/*",
            "Origin": base,
            "Referer": base + "/",
        })
        self.user = cfg.get("user")
        self.csrf = os.environ.get("FREEMAIL_ZM_CSRF") or None
        if self.csrf:
            self.s.headers["X-Zimbra-Csrf-Token"] = self.csrf
        self.token = os.environ.get("FREEMAIL_ZM_TOKEN") or None
        raw_cookie = cookie or os.environ.get("FREEMAIL_COOKIE")  # full Cookie header (auto login or DevTools)
        if raw_cookie:
            self.s.headers["Cookie"] = raw_cookie.strip()
            m = re.search(r"(?:^|;\s*)ZM_AUTH_TOKEN=([^;]+)", raw_cookie)
            self.token = self.token or (m.group(1) if m else None)
        elif self.token:  # reuse a webmail session (cookie ZM_AUTH_TOKEN) when direct AuthRequest is blocked
            host = re.sub(r"^https?://", "", cfg["zimbra_url"]).split("/")[0]
            self.s.cookies.set("ZM_AUTH_TOKEN", self.token, domain=host)

    def call(self, req: str, body: dict, ns: str = "urn:zimbraMail") -> dict:
        # mirrors Free's ZimbraWebClient 7.2 context (authToken as plain string, account by name)
        ctx = {"_jsns": "urn:zimbra",
               "userAgent": {"name": "ZimbraWebClient - GC152 (Mac)", "version": "7.2.0-GA2598"}}
        if self.user:
            ctx["account"] = {"_content": self.user, "by": "name"}
        if self.token:
            ctx["authToken"] = self.token
        if self.csrf:
            ctx["csrfToken"] = self.csrf
        payload = {"Header": {"context": ctx}, "Body": {req: {"_jsns": ns, **body}}}
        r = self.s.post(f"{self.url}/{req}", data=json.dumps(payload), timeout=30)
        if os.environ.get("FREEMAIL_DEBUG"):
            print(f"[debug] POST {self.url}/{req} -> {r.status_code} {r.headers.get('Content-Type')}", file=sys.stderr)
        try:
            j = r.json()
        except ValueError:
            raise ZimbraFault(f"non-JSON response HTTP {r.status_code} from {self.url}: {r.text[:300]!r}") from None
        b = j.get("Body", {})
        if "Fault" in b:
            f = b["Fault"]
            raise ZimbraFault(f"{f.get('Reason', {}).get('Text')} | {json.dumps(f.get('Detail', {}))}")
        return b

    def login(self, user: str, pw: str):
        b = self.call("AuthRequest", {"account": {"by": "name", "_content": user}, "password": {"_content": pw}}, "urn:zimbraAccount")
        tok = b["AuthResponse"]["authToken"]
        self.token = tok[0]["_content"] if isinstance(tok, list) else tok.get("_content", tok)

    def get_rules(self) -> list[dict]:
        b = self.call("GetFilterRulesRequest", {})
        fr = b.get("GetFilterRulesResponse", {}).get("filterRules", [])
        fr = fr[0] if isinstance(fr, list) and fr else fr
        rules = (fr or {}).get("filterRule", [])
        return rules if isinstance(rules, list) else [rules]

    def set_rules(self, rules: list[dict]):
        self.call("ModifyFilterRulesRequest", {"filterRules": [{"filterRule": rules}]})


class LoginError(Exception):
    pass


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


def _cookie_header(cookies, host: str) -> str:
    """cookies: iterable of (name, value, domain). Keep those valid for host."""
    seen = {}
    for name, value, domain in cookies:
        d = (domain or host).lstrip(".")
        if host == d or host.endswith("." + d):
            seen[name] = value
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def _parse_forms(html: str) -> list[dict]:
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms, self.cur = [], None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                self.cur = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(), "inputs": []}
                self.forms.append(self.cur)
            elif tag == "input" and self.cur is not None:
                self.cur["inputs"].append({"name": a.get("name"), "type": (a.get("type") or "text").lower(),
                                           "value": a.get("value") or "", "checked": "checked" in a})

        def handle_endtag(self, tag):
            if tag == "form":
                self.cur = None

    p = P()
    p.feed(html)
    return p.forms


def _http_login(base: str, user: str, pw: str, log) -> str:
    """Submit the webmail login form with plain HTTP (no JavaScript)."""
    import requests
    from urllib.parse import urljoin, urlparse
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    r = s.get(base, timeout=20)
    form = next((f for f in _parse_forms(r.text) if any(i["type"] == "password" for i in f["inputs"])), None)
    if not form:
        raise LoginError("formulaire de connexion introuvable dans la page (page générée en JavaScript ?)")
    data = {}
    for i in form["inputs"]:
        if not i["name"] or i["type"] in ("submit", "button", "image", "reset", "file"):
            continue
        if i["type"] in ("checkbox", "radio") and not i["checked"]:
            continue
        data[i["name"]] = i["value"]
    pw_field = next(i["name"] for i in form["inputs"] if i["type"] == "password")
    user_field = next((i["name"] for i in form["inputs"] if i["type"] in ("text", "email") and i["name"]), None)
    if not user_field:
        raise LoginError("champ identifiant introuvable")
    data[user_field], data[pw_field] = user, pw
    action = urljoin(r.url, form["action"] or r.url)
    log(f"HTTP : envoi du formulaire ({user_field}/{pw_field}) vers {urlparse(action).path or '/'}")
    send = s.post if form["method"] == "post" else s.get
    kw = {"data": data} if form["method"] == "post" else {"params": data}
    send(action, timeout=20, allow_redirects=True, headers={"Referer": r.url, "Origin": base.rstrip("/")}, **kw)
    if "ZM_AUTH_TOKEN" not in s.cookies:
        s.get(base, timeout=20)
    if "ZM_AUTH_TOKEN" not in s.cookies:
        raise LoginError("pas de session après envoi du formulaire (la page de Free exige sans doute JavaScript)")
    host = urlparse(base).hostname
    return _cookie_header(((c.name, c.value, c.domain) for c in s.cookies), host)


def _browser_login(base: str, user: str, pw: str, log, headless: bool = True) -> str:
    """Drive a real browser (installed Chrome, or Playwright's Chromium) through the login page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise LoginError("navigateur automatisé indisponible : pip install playwright") from None
    from urllib.parse import urlparse
    host = urlparse(base).hostname
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=headless)
            log("Navigateur : Google Chrome")
        except Exception:  # noqa: BLE001
            try:
                b = p.chromium.launch(headless=headless)
                log("Navigateur : Chromium (Playwright)")
            except Exception as e:  # noqa: BLE001
                raise LoginError(f"aucun navigateur utilisable ({e}). Installe Chrome ou lance : playwright install chromium") from None
        try:
            ctx = b.new_context(locale="fr-FR", user_agent=UA)
            pg = ctx.new_page()
            pg.goto(base, wait_until="domcontentloaded", timeout=30000)
            pwd = pg.locator("input[type=password]").first
            pwd.wait_for(state="visible", timeout=20000)
            usr = pg.locator("input:not([type=hidden]):not([type=password]):not([type=submit]):not([type=button])"
                             ":not([type=checkbox]):not([type=radio])").first
            usr.fill(user)
            pwd.fill(pw)
            log("Navigateur : formulaire rempli, validation…")
            pwd.press("Enter")
            start_url = pg.url
            for i in range(60):
                if any(c["name"] == "ZM_AUTH_TOKEN" for c in ctx.cookies()):
                    break
                pg.wait_for_timeout(500)
                # still on the same page with the password field after 8 s: credentials refused
                if i >= 16 and pg.url == start_url and pg.locator("input[type=password]").first.is_visible():
                    raise LoginError("identifiants refusés (le formulaire de connexion est toujours affiché)")
            else:
                raise LoginError("pas de session après 30 s (captcha ou double authentification ?)")
            pg.wait_for_timeout(1000)
            return _cookie_header(((c["name"], c["value"], c["domain"]) for c in ctx.cookies()), host)
        finally:
            b.close()


def webmail_login(cfg: dict, password: str | None = None, log=print, method: str | None = None) -> tuple[str, int]:
    """Log into Free's webmail like a browser would and return (Cookie header, number of server filters).

    Tries plain HTTP first, then a real browser. The password defaults to the Keychain one (same as IMAP)."""
    pw = password or get_password(cfg["user"])
    base = cfg["zimbra_url"].rstrip("/") + "/"
    method = method or cfg.get("login_method", "auto")
    users = [cfg.get("webmail_user") or cfg["user"]]
    if "@" in users[0]:
        users.append(users[0].split("@")[0])
    errors = []
    state_file = ROOT / ".login-method"  # remembers which method worked last (no secret inside)
    order = ["http", "browser"]
    if method == "auto" and state_file.exists() and state_file.read_text().strip() == "browser":
        order = ["browser", "http"]
    for how in (order if method == "auto" else [method]):
        for u in users:
            try:
                log(f"Connexion {how} avec l'identifiant « {u} »…")
                cookie = _http_login(base, u, pw, log) if how == "http" else \
                    _browser_login(base, u, pw, log, headless=cfg.get("_headless", True))
                rules = Zimbra(cfg, cookie=cookie).get_rules()
                log(f"Session Zimbra valide ({len(rules)} filtres)")
                try:
                    state_file.write_text(how)
                except OSError:
                    pass
                return cookie, len(rules)
            except (LoginError, ZimbraFault) as e:
                errors.append(f"{how}/{u} : {e}")
                log(f"  échec : {e}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{how}/{u} : {type(e).__name__}: {e}")
                log(f"  échec : {type(e).__name__}: {e}")
    if errors and all("refusés" in e for e in errors):
        raise LoginError("Identifiant ou mot de passe refusé par Free. Détail : " + " | ".join(errors))
    raise LoginError("Connexion automatique impossible (captcha, double authentification ou page modifiée ?). "
                     "Utilise la méthode manuelle. Détail : " + " | ".join(errors))


def yaml_to_rule(f: dict) -> dict:
    """Convert the simplified YAML filter DSL into a Zimbra filterRule (JSON)."""
    tests = defaultdict(list)
    idx = 0
    for c in f.get("conditions", []):
        if isinstance(c, str):
            raise ValueError(f"bad condition {c!r}")
        (k, v), = c.items()
        neg = {}
        if k.startswith("not_"):
            k, neg = k[4:], {"negative": True}
        if k in ("from_contains", "to_contains", "cc_contains", "subject_contains"):
            hdr = k.split("_")[0]
            tests["headerTest"].append({"index": idx, "header": hdr, "stringComparison": "contains", "value": v, **neg})
        elif k in ("from_is", "subject_is"):
            tests["headerTest"].append({"index": idx, "header": k.split("_")[0], "stringComparison": "is", "value": v, **neg})
        elif k == "header_contains":
            tests["headerTest"].append({"index": idx, "header": v["name"], "stringComparison": "contains", "value": v["value"], **neg})
        elif k == "header_exists":
            tests["headerExistsTest"].append({"index": idx, "header": v, **neg})
        elif k == "size_over_kb":
            tests["sizeTest"].append({"index": idx, "numberComparison": "over", "s": f"{int(v)}K", **neg})
        else:
            raise ValueError(f"unsupported condition {k!r} in filter {f.get('name')}")
        idx += 1
    acts = defaultdict(list)
    idx = 0
    for a in f.get("actions", []):
        if isinstance(a, str):
            k, v = a, None
        else:
            (k, v), = a.items()
        if k == "fileinto":
            acts["actionFileInto"].append({"index": idx, "folderPath": v})
        elif k == "mark_read":
            acts["actionFlag"].append({"index": idx, "flagName": "read"})
        elif k == "flag":
            acts["actionFlag"].append({"index": idx, "flagName": "flagged"})
        elif k == "tag":
            acts["actionTag"].append({"index": idx, "tagName": v})
        elif k == "redirect":
            acts["actionRedirect"].append({"index": idx, "a": v})
        elif k == "keep":
            acts["actionKeep"].append({"index": idx})
        elif k == "discard":
            acts["actionDiscard"].append({"index": idx})
        else:
            raise ValueError(f"unsupported action {k!r} in filter {f.get('name')}")
        idx += 1
    if f.get("stop", True):
        acts["actionStop"].append({"index": idx})
    return {
        "name": f["name"],
        "active": bool(f.get("active", True)),
        "filterTests": [{"condition": "anyof" if f.get("any") else "allof", **tests}],
        "filterActions": [dict(acts)],
    }


def _one(x):
    return x[0] if isinstance(x, list) and x else (x or {})


def server_filter_to_rule(r: dict, folders: list[str]) -> tuple[dict | None, str]:
    """Translate a Zimbra filter (header contains/is on from/to/cc/subject) to an IMAP clean rule."""
    t, a = _one(r.get("filterTests")), _one(r.get("filterActions"))
    anyof = t.get("condition", "anyof") == "anyof"
    terms, skipped = [], 0
    for k, v in t.items():
        if k == "condition":
            continue
        for h in (v if isinstance(v, list) else [v]):
            hdrs = [x.strip().lower() for x in h.get("header", "").split(",")] if k == "headerTest" else []
            ok = (k == "headerTest" and not h.get("negative") and h.get("stringComparison") in ("contains", "is")
                  and all(x in ("from", "to", "cc", "subject") for x in hdrs) and str(h.get("value", "")).isascii())
            if not ok:
                skipped += 1
                continue
            val = str(h["value"]).lstrip("*")
            parts = [f"{x.upper()} {quote(val)}" for x in hdrs]
            terms.append(_or(parts))
    if not terms or (skipped and not anyof):
        return None, "conditions non vérifiables par recherche IMAP (en-tête technique, « existe », « NE PAS » ou accents)"
    crit = _or(terms) if anyof else " ".join(terms)
    if a.get("actionFileInto"):
        action = {"move": _one(a["actionFileInto"])["folderPath"].lstrip("/")}
    elif a.get("actionDiscard"):
        action = "trash"  # never hard-delete retroactively
    else:
        return None, "no fileinto/discard action"
    return {"name": r["name"], "folders": folders, "_criteria": crit, "action": action}, \
        (f"{skipped} test(s) ignored" if skipped else "")


def _norm(rule: dict) -> str:
    return json.dumps(rule, sort_keys=True)


def plan_filters(current: list[dict], desired: list[dict], replace: bool):
    cur = {r["name"]: r for r in current}
    des = {r["name"]: r for r in desired}
    changes, result = [], []
    for r in current:  # keep existing order; update in place
        n = r["name"]
        if n in des:
            same = _norm(r) == _norm(des[n])
            changes.append(("=" if same else "~", n))
            result.append(r if same else des[n])
        elif replace:
            changes.append(("-", n))
        else:
            changes.append(("keep", n))
            result.append(r)
    for r in desired:
        if r["name"] not in cur:
            changes.append(("+", r["name"]))
            result.append(r)
    return changes, result


def cmd_filters_run(cfg, args):
    src = args.file if args.file.endswith(".json") else str(sorted(BACKUPS.glob("filters-*.json"))[-1])
    import fnmatch
    rules, notes = [], []
    for r in json.loads(Path(src).read_text()):
        if not r.get("active"):
            continue
        if args.name and not any(fnmatch.fnmatch(r["name"], pat) for pat in args.name):
            continue
        rule, note = server_filter_to_rule(r, args.folder or ["INBOX"])
        if rule:
            rules.append(rule)
        if note:
            notes.append(f"  {r['name']}: {note}")
    print(f"source: {src} - {len(rules)} filter(s) replayable via IMAP")
    if notes:
        print("notes:\n" + "\n".join(notes))
    args.only, args.yes = None, getattr(args, "yes", False)
    run_rules(cfg, {"trash_folder": "auto", "rules": rules}, args)


def cmd_filters(cfg, args):
    if args.action == "run":
        return cmd_filters_run(cfg, args)
    z = Zimbra(cfg)
    try:
        if not z.token:
            cookie, _ = webmail_login(cfg, log=lambda m: print(f"[login] {m}", file=sys.stderr))
            z = Zimbra(cfg, cookie=cookie)
        current = z.get_rules()
    except LoginError as e:
        sys.exit(f"Login error: {e}\nFallback: export FREEMAIL_COOKIE (see README)")
    except ZimbraFault as e:
        sys.exit(f"Zimbra SOAP error: {e}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Zimbra SOAP unreachable ({type(e).__name__}): {e}")

    if args.action == "test":
        print(f"SOAP OK on {z.url} - {len(current)} server filter(s): " + ", ".join(r.get("name", "?") for r in current))
        return
    BACKUPS.mkdir(exist_ok=True)
    if args.action == "pull":
        p = BACKUPS / f"filters-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        p.write_text(json.dumps(current, indent=2, ensure_ascii=False))
        print(f"{len(current)} filter(s) saved to {p.relative_to(ROOT)}")
        for r in current:
            print(f"  [{'on' if r.get('active') else 'off'}] {r.get('name')}")
        return
    if args.action == "restore":
        rules = json.loads(Path(args.file).read_text())
        bk = BACKUPS / f"filters-before-restore-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        bk.write_text(json.dumps(current, indent=2, ensure_ascii=False))
        print(f"current filters backed up to {bk.relative_to(ROOT)}")
        if input(f"Replace {len(current)} server filters with {len(rules)} from {args.file}? [y/N] ").lower() != "y":
            sys.exit("aborted")
        z.set_rules(rules)
        print("restored")
        return

    spec = yaml.safe_load(Path(args.file).read_text()) or {}
    desired = [yaml_to_rule(f) for f in spec.get("filters", [])]
    changes, result = plan_filters(current, desired, args.replace)
    for op, n in changes:
        print(f"  {op:>4}  {n}")
    if args.verbose:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.action == "plan" or not args.apply:
        print("\nPLAN only. Use `filters push -f ... --apply` to write to server.")
        return
    if not any(op in ("+", "~", "-") for op, _ in changes):
        print("nothing to change")
        return
    p = BACKUPS / f"filters-before-push-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(current, indent=2, ensure_ascii=False))
    z.set_rules(result)
    print(f"pushed {len(result)} filter(s); backup: {p.relative_to(ROOT)}")


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(prog="freemail", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("auth", help="store password in keyring and test IMAP login")
    p.add_argument("--check", action="store_true", help="only test login")
    sp.add_parser("folders", help="list folders with counts")

    p = sp.add_parser("report", help="read-only mailbox statistics")
    p.add_argument("--folder", action="append", help="limit to folder (repeatable)")
    p.add_argument("--since", type=int, help="only last N days")
    p.add_argument("--min-newsletter", type=int, default=10, help="min msgs for rule suggestion")

    p = sp.add_parser("clean", help="apply cleanup rules (dry-run by default)")
    p.add_argument("-r", "--rules", default=str(ROOT / "rules.yaml"))
    p.add_argument("--only", action="append", help="run only rules matching NAME or glob, e.g. 'route-*' (repeatable)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true", help="no confirmation prompt")
    p.add_argument("--samples", type=int, default=8)

    p = sp.add_parser("login", help="test automatic webmail login (Keychain password)")
    p.add_argument("--method", choices=["auto", "http", "browser"], default=None)
    p.add_argument("--show", action="store_true", help="browser method: show the window (debug)")

    p = sp.add_parser("mkdir", help="create folder(s), e.g. 'Emploi/Alertes'")
    p.add_argument("names", nargs="+")

    p = sp.add_parser("prune", help="delete empty folders (dry-run by default)")
    p.add_argument("--exclude", action="append", help="folder to keep (repeatable)")
    p.add_argument("--apply", action="store_true")

    p = sp.add_parser("filters", help="Zimbra server-side filters")
    p.add_argument("action", choices=["test", "pull", "plan", "push", "restore", "run"])
    p.add_argument("--name", action="append", help="run: only filters matching NAME/glob")
    p.add_argument("--folder", action="append", help="run: folders to replay on (default INBOX)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("-f", "--file", default=str(ROOT / "filters.yaml"), help="filters.yaml (plan/push) or backup json (restore)")
    p.add_argument("--replace", action="store_true", help="delete server filters not declared in file")
    p.add_argument("--apply", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    {"auth": cmd_auth, "folders": cmd_folders, "report": cmd_report, "clean": cmd_clean, "prune": cmd_prune, "mkdir": cmd_mkdir, "login": cmd_login, "filters": cmd_filters}[args.cmd](cfg, args)


if __name__ == "__main__":
    try:
        main()
    except imaplib.IMAP4.error as e:
        sys.exit(f"IMAP error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
