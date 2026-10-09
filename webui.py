#!/usr/bin/env python3
"""freemail web UI - local dashboard for a Free (Zimbra) mailbox.

Runs on 127.0.0.1 only, protected by a random per-run token. The Zimbra session cookie
is kept in memory only (never written to disk). IMAP uses the password stored in the
macOS Keychain by `./freemail.py auth`.

    ./webui.py            # opens http://127.0.0.1:8765/?t=<token>
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

import extras
import freemail as fm
import mailweb

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
TOKEN = secrets.token_urlsafe(24)
extras.init(lambda: ROOT)  # ROOT is re-pointed to the app's files dir on Android
APP_VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "dev"
STATE: dict = {"cookie": None, "user": None, "cfg": None, "imap": False, "notifier": None}
LAST_VISIBLE = [0.0]  # last poll from a visible UI page (silences phone notifications)
# URL -> (file under web/, content type). Only these files are served: the URL is a lookup key, never a path.
STATIC = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/vendor/chart.umd.min.js": ("vendor/chart.umd.min.js", "text/javascript; charset=utf-8"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/maskable-512.png": ("icons/maskable-512.png", "image/png"),
    "/icons/badge-96.png": ("icons/badge-96.png", "image/png"),
    "/offline.html": ("offline.html", "text/html; charset=utf-8"),
}
JOBS: dict[str, "Job"] = {}
PLANS: dict[str, dict] = {}
IMAP_LOCK = threading.Lock()  # one mutating IMAP operation at a time
READ_ONLY = __import__("contextlib").nullcontext  # reads use their own IMAP connection, no lock
PLAN_TTL = 900


class ApiError(Exception):
    def __init__(self, status: int, msg: str, extra: dict | None = None):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra or {}


# --------------------------------------------------------------------------- jobs
class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.status, self.logs = kind, "running", []
        self.result, self.error, self.started = None, None, time.time()
        self.progress = None  # {"done", "total", "label", "messages"}

    def set(self, frac: float, label: str = "", detail: str = ""):
        self.progress = {"pct": round(max(0.0, min(1.0, frac)) * 100, 1), "label": label, "detail": detail}

    def log(self, msg):
        self.logs.append(f"{dt.datetime.now():%H:%M:%S} {msg}")

    def view(self):
        return {"id": self.id, "kind": self.kind, "status": self.status, "logs": self.logs[-200:],
                "result": self.result, "error": self.error, "elapsed": round(time.time() - self.started, 1),
                "progress": self.progress}


def start_job(kind: str, fn) -> Job:
    job = Job(kind)
    JOBS[job.id] = job

    def run():
        try:
            job.result = fn(job)
            job.status = "done"
        except Exception as e:  # noqa: BLE001
            job.error = f"{type(e).__name__}: {e}"
            job.log(traceback.format_exc(limit=3))
            job.status = "error"

    threading.Thread(target=run, daemon=True).start()
    return job


# --------------------------------------------------------------------------- helpers
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "")


def _has_playwright() -> bool:
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


def cfg() -> dict:
    if STATE["cfg"] is None:
        STATE["cfg"] = fm.load_config(str(ROOT / "config.yaml"))
    return STATE["cfg"]


def mailbox() -> fm.Mailbox:
    return fm.Mailbox(cfg(), password=STATE.get("password"))


MAIL = mailweb.Conn(lambda: cfg(), lambda: STATE.get("password"))


def mail(fn):
    """Run fn(mb) on the shared webmail IMAP connection, mapping errors to API errors."""
    try:
        return MAIL.run(fn)
    except mailweb.MailError as e:
        raise ApiError(400, str(e)) from None
    except RuntimeError as e:
        raise ApiError(400, str(e)) from None
    except SystemExit as e:  # freemail.py reports config/password problems with sys.exit()
        raise ApiError(400, str(e)) from None
    except OSError as e:  # no network / server unreachable: the UI falls back to its offline cache
        raise ApiError(503, _L(f"Boîte mail injoignable : {e}", f"Mailbox unreachable: {e}")) from None


def _zimbra_missing_msg() -> str:
    if STATE.get("android"):
        return "Filtres serveur : connecte-toi au webmail Free (bouton ci-dessous) pour les gérer depuis le téléphone."
    return "Mode boîte mail seule : les filtres serveur Zimbra demandent une session webmail (connexion depuis le Mac)."


def zimbra() -> fm.Zimbra:
    if not STATE["cookie"] and STATE.get("imap"):
        raise ApiError(409, _zimbra_missing_msg(), {"zimbra_login": bool(STATE.get("android"))})
    if not STATE["cookie"]:
        raise ApiError(401, "Session Zimbra absente : colle ton cookie.")
    os.environ["FREEMAIL_COOKIE"] = STATE["cookie"]
    try:
        return fm.Zimbra(cfg())
    finally:
        os.environ.pop("FREEMAIL_COOKIE", None)


def relogin() -> bool:
    """Automatic mode: reopen an expired webmail session with the stored credentials."""
    if not STATE.get("auto"):
        return False
    try:
        STATE["cookie"], _ = fm.webmail_login(cfg(), password=STATE.get("password"), log=lambda m: None)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_rules() -> list[dict]:
    try:
        return zimbra().get_rules()
    except fm.ZimbraFault as e:
        if relogin():
            return zimbra().get_rules()
        if STATE.get("imap"):  # mailbox session still valid: only the webmail part expired
            STATE["cookie"] = None
            raise ApiError(409, f"Session webmail expirée ({e}). " + _zimbra_missing_msg(), {"zimbra_login": bool(STATE.get("android"))}) from None
        raise ApiError(401, f"Session Zimbra expirée ou refusée, reconnecte-toi : {e}") from None


def backup_rules(rules: list[dict], tag: str) -> str:
    fm.BACKUPS.mkdir(exist_ok=True)
    p = fm.BACKUPS / f"filters-{tag}-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(rules, indent=2, ensure_ascii=False))
    return p.name


def set_rules(new: list[dict], current: list[dict], tag: str) -> str:
    name = backup_rules(current, f"before-{tag}")
    zimbra().set_rules(new)
    return name


def row_view(r: dict) -> dict:
    return {"uid": r["uid"], "date": r["date"].strftime("%Y-%m-%d") if r.get("date") else "",
            "from": r["from"], "subject": r["subject"], "size": r["size"]}


def store_plan(kind: str, payload: dict) -> str:
    now = time.time()
    for k in [k for k, v in PLANS.items() if now - v["ts"] > PLAN_TTL]:
        PLANS.pop(k, None)
    pid = uuid.uuid4().hex[:12]
    PLANS[pid] = {"ts": now, "kind": kind, **payload}
    return pid


def plan_view(plan: list[dict]) -> list[dict]:
    return [{"rule": p["rule"].get("name"), "folder": p["folder"], "action": p["action"], "criteria": p["criteria"],
             "count": len(p["rows"]), "size": sum(r["size"] for r in p["rows"]),
             "samples": [row_view(r) for r in p["rows"][:15]]} for p in plan]


# --------------------------------------------------------------------------- filters: model <-> zimbra
FIELDS = {"from": "Expéditeur", "to": "Destinataire", "cc": "Copie", "subject": "Sujet"}


def rule_summary(r: dict, pos: int) -> dict:
    t, a = fm._one(r.get("filterTests")), fm._one(r.get("filterActions"))
    conds = []
    for k, v in t.items():
        if k == "condition":
            continue
        for h in (v if isinstance(v, list) else [v]):
            conds.append({"type": k.replace("Test", ""), "header": h.get("header", ""), "op": h.get("stringComparison") or
                          h.get("numberComparison") or ("exists" if k == "headerExistsTest" else ""),
                          "value": h.get("value") or h.get("s") or "", "negative": str(h.get("negative", "")) in ("1", "true", "True")})
    acts = []
    for k, v in a.items():
        for x in (v if isinstance(v, list) else [v]):
            if k == "actionFileInto":
                acts.append({"type": "fileinto", "folder": x.get("folderPath", "").lstrip("/")})
            elif k == "actionFlag":
                acts.append({"type": "mark_read" if x.get("flagName") == "read" else "flag"})
            elif k == "actionDiscard":
                acts.append({"type": "discard"})
            elif k == "actionStop":
                acts.append({"type": "stop"})
            elif k == "actionKeep":
                acts.append({"type": "keep"})
            elif k == "actionTag":
                acts.append({"type": "tag", "tag": x.get("tagName")})
            elif k == "actionRedirect":
                acts.append({"type": "redirect", "to": x.get("a")})
            else:
                acts.append({"type": k})
    return {"position": pos, "name": r.get("name"), "active": bool(r.get("active")),
            "any": t.get("condition", "anyof") == "anyof", "conditions": conds, "actions": acts}


def model_to_rule(m: dict) -> dict:
    """UI model -> Zimbra filterRule. Validates input."""
    name = (m.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Le filtre doit avoir un nom.")
    tests, i = {}, 0
    for c in m.get("conditions", []):
        field = c.get("field")
        header = (c.get("header") or "").strip() if field == "header" else field
        op, val = c.get("op", "contains"), (c.get("value") or "").strip()
        if not header:
            raise ApiError(400, "Condition : nom d'en-tête manquant.")
        neg = {"negative": True} if c.get("negative") else {}
        if op == "exists":
            tests.setdefault("headerExistsTest", []).append({"index": i, "header": header, **neg})
        else:
            if not val:
                raise ApiError(400, f"Condition sur « {header} » : valeur vide.")
            if op not in ("contains", "is", "matches"):
                raise ApiError(400, f"Opérateur inconnu : {op}")
            tests.setdefault("headerTest", []).append({"index": i, "header": header, "stringComparison": op, "value": val, **neg})
        i += 1
    if not tests:
        raise ApiError(400, "Ajoute au moins une condition.")
    acts, i = {}, 0
    for a in m.get("actions", []):
        t = a.get("type")
        if t == "fileinto":
            folder = (a.get("folder") or "").strip().strip("/")
            if not folder:
                raise ApiError(400, "Action « déplacer » : dossier vide.")
            acts.setdefault("actionFileInto", []).append({"index": i, "folderPath": folder})
        elif t == "junk":
            acts.setdefault("actionFileInto", []).append({"index": i, "folderPath": "Junk"})
        elif t == "mark_read":
            acts.setdefault("actionFlag", []).append({"index": i, "flagName": "read"})
        elif t == "flag":
            acts.setdefault("actionFlag", []).append({"index": i, "flagName": "flagged"})
        elif t == "discard":
            acts.setdefault("actionDiscard", []).append({"index": i})
        elif t == "keep":
            acts.setdefault("actionKeep", []).append({"index": i})
        else:
            raise ApiError(400, f"Action inconnue : {t}")
        i += 1
    if not acts:
        raise ApiError(400, "Ajoute au moins une action.")
    if m.get("stop", True):
        acts.setdefault("actionStop", []).append({"index": i})
    return {"name": name, "active": bool(m.get("active", True)),
            "filterTests": [{"condition": "anyof" if m.get("any", True) else "allof", **tests}],
            "filterActions": [acts]}


def _tests(rule: dict) -> list[tuple[str, str, str]]:
    """(header, op, value) for positive header tests, header split on ','."""
    t = fm._one(rule.get("filterTests"))
    out = []
    for h in t.get("headerTest", []) if isinstance(t.get("headerTest"), list) else [t.get("headerTest")] if t.get("headerTest") else []:
        if str(h.get("negative", "")) in ("1", "true", "True"):
            continue
        for hd in str(h.get("header", "")).lower().split(","):
            out.append((hd.strip(), h.get("stringComparison", ""), str(h.get("value", "")).lower().strip("*")))
    for h in t.get("headerExistsTest", []) if isinstance(t.get("headerExistsTest"), list) else []:
        out.append((str(h.get("header", "")).lower(), "exists", ""))
    return out


def _has_stop(rule: dict) -> bool:
    return bool(fm._one(rule.get("filterActions")).get("actionStop"))


def _dest(rule: dict) -> str:
    a = fm._one(rule.get("filterActions"))
    if a.get("actionDiscard"):
        return "SUPPRESSION"
    if a.get("actionFileInto"):
        return fm._one(a["actionFileInto"]).get("folderPath", "").lstrip("/")
    return "boîte de réception"


def insert_index(rules: list[dict], position: str) -> int:
    if position == "top":
        return 0
    if position and position.startswith("before:"):
        target = position.split(":", 1)[1]
        for i, r in enumerate(rules):
            if r.get("name") == target:
                return i
    return len(rules)


def check_filter(new: dict, current: list[dict], position: str, replace: str | None, folders: set[str]) -> dict:
    issues = []

    def add(level, msg, **kw):
        issues.append({"level": level, "msg": msg, **kw})

    others = [r for r in current if r.get("name") != replace]
    if any(r.get("name") == new["name"] for r in others):
        add("error", f"Un filtre nommé « {new['name']} » existe déjà. Choisis un autre nom ou modifie l'existant.")
    idx = insert_index(others, position)
    new_tests, new_dest, new_stop = _tests(new), _dest(new), _has_stop(new)
    for pos, r in enumerate(others):
        if not r.get("active"):
            continue
        for (h1, op1, v1) in new_tests:
            for (h2, op2, v2) in _tests(r):
                if h1 != h2:
                    continue
                if "exists" in (op1, op2):
                    rel = "identique" if op1 == op2 else ("plus_large" if op2 == "exists" else "plus_precis")
                elif v1 == v2:
                    rel = "identique"
                elif v2 and v2 in v1:
                    rel = "plus_large"   # existing catches more than the new one
                elif v1 and v1 in v2:
                    rel = "plus_precis"  # existing is a subset of the new one
                else:
                    continue
                before = pos < idx
                same_dest = _dest(r) == new_dest
                label = f"« {r['name']} » (position {pos + 1}, {h2} {op2} « {v2} » → {_dest(r)})"
                if before and _has_stop(r) and rel in ("identique", "plus_large"):
                    add("error" if not same_dest else "info",
                        f"Masqué : {label} est placé avant et attrape déjà ces mails, ton filtre ne sera jamais atteint."
                        + (" Il les range déjà au même endroit : ce filtre est inutile." if same_dest else
                           " Place ton filtre avant, ou modifie l'existant."), filter=r["name"])
                elif before and _has_stop(r) and rel == "plus_precis" and not same_dest:
                    add("warning", f"Partiellement masqué : {label} est placé avant ; les mails qui correspondent à « {v2} » "
                                   f"iront vers « {_dest(r)} », pas vers « {new_dest} ».", filter=r["name"])
                elif not before and new_stop and rel in ("identique", "plus_precis"):
                    add("warning" if not same_dest else "info",
                        f"Interception : ton filtre placé avant {label} va lui prendre ces mails"
                        + (" (même destination, sans effet)." if same_dest else f" et les envoyer vers « {new_dest} »."),
                        filter=r["name"])
                elif rel != "identique" or not same_dest:
                    add("info", f"Chevauchement avec {label}.", filter=r["name"])
                else:
                    add("info", f"Doublon de {label}.", filter=r["name"])
    a = fm._one(new.get("filterActions"))
    if a.get("actionDiscard"):
        add("warning", "Suppression définitive à la réception : en cas de faux positif le mail est perdu. Préfère « Spam (Junk) ».")
    dest = fm._one(a.get("actionFileInto")).get("folderPath", "").lstrip("/") if a.get("actionFileInto") else None
    if dest and dest not in folders:
        add("warning", f"Le dossier « {dest} » n'existe pas : il sera créé à l'enregistrement.", create=dest)
    for (h, op, v) in new_tests:
        if op == "contains" and v and len(v) < 4:
            add("warning", f"Valeur très courte « {v} » sur {h} : risque d'attraper beaucoup de mails non voulus.")
    seen = {}
    for i in issues:
        seen.setdefault(i["msg"], i)
    issues = list(seen.values())
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda x: order[x["level"]])
    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues, "insert_at": idx + 1}


# --------------------------------------------------------------------------- maintenance catalogue
def rules_yaml() -> dict:
    p = ROOT / "rules.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {"rules": []}


MAINT = {
    "newsletters": {
        "title": "Ranger les vieilles newsletters",
        "desc": "Déplace de la boîte de réception vers « Newsletters » les mails de plus de 60 jours qui ont un lien de "
                "désabonnement (en-tête List-Unsubscribe). Rien n'est supprimé.",
        "danger": False,
        "spec": lambda: {"rules": [{"name": "newsletters-older-60d", "folders": ["INBOX"],
                                    "match": {"newsletter": True, "older_than_days": 60}, "action": {"move": "Newsletters"}}]},
    },
    "livraisons": {
        "title": "Vider les vieux suivis de colis",
        "desc": "Envoie à la Corbeille les mails du dossier « Livraisons » de plus de 90 jours (suivis Colissimo, "
                "Chronopost, UPS…), inutiles une fois le colis reçu. Récupérables dans la Corbeille.",
        "danger": False,
        "spec": lambda: {"rules": [{"name": "livraisons-old", "folders": ["Livraisons"], "match": {"older_than_days": 90},
                                    "action": "trash"}]},
    },
    "notifications": {
        "title": "Purger les vieilles notifications",
        "desc": "Applique les règles « *-old » de rules.yaml : notifications GitHub > 90 j, alertes LinkedIn > 30 j, "
                "notifications bancaires > 1 an, suivis Amazon > 6 mois → Corbeille.",
        "danger": False,
        "spec": lambda: {**rules_yaml(), "rules": [r for r in rules_yaml().get("rules", [])
                                                   if r.get("name", "").endswith("-old") and r.get("name") != "livraisons-old"]},
    },
    "replay": {
        "title": "Rejouer les filtres sur la boîte de réception",
        "desc": "Applique tes filtres serveur aux mails déjà présents dans la boîte de réception (Zimbra ne les applique "
                "qu'aux nouveaux mails). Les filtres « supprimer » deviennent « Corbeille ». Nécessite la session Zimbra.",
        "danger": False,
        "spec": None,  # built from server filters
    },
    "empty_trash": {
        "title": "Vider la Corbeille",
        "desc": "Supprime DÉFINITIVEMENT tous les mails de la Corbeille. Irréversible.",
        "danger": True, "folder": ("\\Trash", ["Trash", "Corbeille"]),
    },
    "empty_junk": {
        "title": "Vider le dossier Spam",
        "desc": "Supprime DÉFINITIVEMENT tous les mails du dossier Junk (spam). Vérifie d'abord qu'aucun vrai mail n'y est.",
        "danger": True, "folder": ("\\Junk", ["Junk", "Spam", "Courrier indésirable"]),
    },
    "prune": {
        "title": "Supprimer les dossiers vides",
        "desc": "Supprime les dossiers qui ne contiennent aucun mail. Les dossiers système et ceux utilisés comme destination par un filtre ou une règle sont conservés.",
        "danger": False,
    },
}

PROTECTED = {"inbox", "sent", "drafts", "trash", "junk", "contacts", "emailed contacts", "chats", "newsletters", "livraisons"}


def maint_plan(key: str, job: Job) -> dict:
    m = MAINT[key]
    job.set(0, "Connexion à la boîte…")
    with READ_ONLY():
        mb = mailbox()
        job.log("Connecté à la boîte (IMAP)")
        try:
            if key in ("empty_trash", "empty_junk"):
                flag, fallbacks = m["folder"]
                raw = mb.special(flag, fallbacks)
                if not raw:
                    raise ApiError(404, "Dossier introuvable.")
                mb.select(raw)
                uids = mb.search("ALL")
                job.log(f"{fm.utf7_decode(raw)} : {len(uids)} mails")
                rows = mb.fetch(uids, on_chunk=lambda d, t: job.set(d / t if t else 1, fm.utf7_decode(raw), f"lecture des en-têtes {d}/{t}"))
                rows.sort(key=lambda r: r["date"] or dt.datetime.min, reverse=True)
                pid = store_plan(key, {"raw": raw, "count": len(rows)})
                return {"plan_id": pid, "groups": [{"rule": m["title"], "folder": fm.utf7_decode(raw), "action": "suppression définitive",
                                                    "count": len(rows), "size": sum(r["size"] for r in rows),
                                                    "samples": [row_view(r) for r in rows[:15]]}], "total": len(rows)}
            if key == "prune":
                names = {f["name"] for f in mb.folders}
                used = set()
                if STATE["cookie"]:
                    for r in get_rules():
                        used |= {x["folder"].lower() for x in rule_summary(r, 0)["actions"] if x["type"] == "fileinto"}
                for r in rules_yaml().get("rules", []):
                    if isinstance(r.get("action"), dict) and r["action"].get("move"):
                        used.add(r["action"]["move"].lower())
                job.log(f"{len(used)} dossiers protégés (destinations de filtres/règles)")
                cands = []
                ordered = sorted(mb.folders, key=lambda x: x["name"].count(x["delim"]), reverse=True)
                for n_, f in enumerate(ordered):
                    job.set(n_ / len(ordered), "Analyse des dossiers", f"{n_}/{len(ordered)} · {f['name']}")
                    if f["name"].lower() in PROTECTED or f["name"].lower() in used or {x.lower() for x in f["flags"]} & {"\\trash", "\\sent", "\\drafts", "\\junk"}:
                        continue
                    if "\\noselect" in [x.lower() for x in f["flags"]]:
                        n = 0
                    else:
                        typ, d = mb.M.status(fm.quote(f["raw"]), "(MESSAGES)")
                        n = int(fm.re.search(r"MESSAGES (\d+)", d[0].decode()).group(1)) if typ == "OK" else -1
                    children = [x for x in names if x.startswith(f["name"] + f["delim"])]
                    if n == 0 and all(c in [y["name"] for y in cands] for c in children):
                        cands.append(f)
                pid = store_plan(key, {"raws": [f["raw"] for f in cands]})
                return {"plan_id": pid, "total": len(cands), "groups": [
                    {"rule": "Dossiers vides", "folder": "", "action": "suppression du dossier", "count": len(cands), "size": 0,
                     "samples": [{"uid": "", "date": "", "from": f["name"], "subject": "", "size": 0} for f in cands]}]}
            if key == "replay":
                job.set(0, "Lecture de tes filtres serveur…")
                rules = []
                for r in get_rules():
                    if r.get("active"):
                        rule, _ = fm.server_filter_to_rule(r, ["INBOX"])
                        if rule:
                            rules.append(rule)
                spec = {"trash_folder": "auto", "rules": rules}
            else:
                spec = m["spec"]()
            plan, trash = fm.plan_rules(mb, spec, log=job.log, progress=job.set)
            plan = [p for p in plan if p["rows"]]
            pid = store_plan(key, {"plan": plan, "trash": trash})
            return {"plan_id": pid, "groups": plan_view(plan), "total": sum(len(p["rows"]) for p in plan)}
        finally:
            mb.close()


def maint_apply(pid: str, job: Job) -> dict:
    p = PLANS.pop(pid, None)
    if not p:
        raise ApiError(410, "Simulation expirée : relance-la.")
    job.set(0, "Connexion à la boîte…")
    with IMAP_LOCK:
        mb = mailbox()
        try:
            if p["kind"] in ("empty_trash", "empty_junk"):
                job.set(.3, "Suppression définitive en cours…", f"{p['count']} mails")
                n = mb.empty(p["raw"])
                job.set(1, "Terminé", f"{n} mails supprimés")
                job.log(f"{n} mails supprimés définitivement de {fm.utf7_decode(p['raw'])}")
                return {"done": n}
            if p["kind"] == "prune":
                ok = 0
                for n_, raw in enumerate(p["raws"]):
                    job.set(n_ / max(1, len(p["raws"])), "Suppression des dossiers vides", fm.utf7_decode(raw))
                    typ, d = mb.M.delete(fm.quote(raw))
                    job.log(f"{'supprimé' if typ == 'OK' else 'échec'} : {fm.utf7_decode(raw)}")
                    ok += typ == "OK"
                return {"done": ok}
            path = fm.apply_plan(mb, p["plan"], p["trash"], log=job.log, progress=job.set)
            return {"done": sum(len(x["rows"]) for x in p["plan"]), "log": path.name}
        finally:
            mb.close()


# --------------------------------------------------------------------------- HTTP
def latest_stats() -> dict | None:
    reps = sorted(fm.OUT.glob("report-*/stats.json"))
    return json.loads(reps[-1].read_text()) if reps else None


class Handler(BaseHTTPRequestHandler):
    server_version = "freemail"

    def log_message(self, fmt, *args):  # quiet
        if os.environ.get("FREEMAIL_DEBUG"):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    # ---- plumbing
    def _send(self, status: int, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 40_000_000:
            raise ApiError(413, "requête trop grosse")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        if not self._host_ok():
            return self._send(403, {"error": "host refusé"})
        url = urlparse(self.path)
        if method == "GET" and url.path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
        if method == "GET" and url.path in STATIC:
            extra = {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"} if url.path == "/sw.js" else {"Cache-Control": "max-age=86400"}
            fname, ctype = STATIC[url.path]
            return self._send(200, (WEB / fname).read_bytes(), ctype, extra)
        if not url.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return self._send(403, {"error": "Jeton invalide : rouvre l'URL affichée dans le terminal."})
        try:
            route = getattr(self, f"api_{method.lower()}_{url.path[5:].replace('/', '_')}", None)
            if not route:
                raise ApiError(404, "route inconnue")
            args = self._body() if method == "POST" else {k: v[0] for k, v in parse_qs(url.query).items()}
            res = route(args)
            if isinstance(res, tuple) and res and res[0] == "__binary__":
                _, data, ctype, fname = res
                from urllib.parse import quote as urlq
                return self._send(200, data, ctype, {"Content-Disposition": f"attachment; filename*=UTF-8''{urlq(fname)}"})
            self._send(200, res)
        except ApiError as e:
            self._send(e.status, {"error": e.msg, **e.extra})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            extras.diag_exc(f"API {url.path}", e)
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    # ---- session
    def api_get_session(self, a):
        return {"logged": bool(STATE["cookie"] or STATE.get("imap")), "user": cfg().get("user"), "version": APP_VERSION, "auto": bool(STATE.get("auto")),
                "zimbra": bool(STATE["cookie"]), "browser_login": _has_playwright(), "notifier": STATE.get("notifier"),
                "platform": "android" if STATE.get("android") else "termux" if IS_TERMUX else sys.platform}

    def api_post_login(self, a):
        if a.get("mode") == "imap":
            pw = a.get("password") or None
            try:
                fm.Mailbox(cfg(), password=pw).close()
            except SystemExit as e:
                raise ApiError(400, f"{e}. Saisis ton mot de passe Free.") from None
            except Exception as e:  # noqa: BLE001
                raise ApiError(400, f"Connexion IMAP refusée : {e}") from None
            STATE.update(cookie=None, auto=False, imap=True, password=pw)
            MAIL.reset()
            return {"ok": True, "filters": None, "user": cfg()["user"]}
        if a.get("mode") == "auto":
            logs = []
            try:
                cookie, n = fm.webmail_login(cfg(), password=a.get("password") or None, log=logs.append)
            except SystemExit as e:  # no Keychain password
                raise ApiError(400, f"{e}. Saisis ton mot de passe ou lance ./freemail.py auth", {"logs": logs}) from None
            except fm.LoginError as e:
                raise ApiError(400, str(e), {"logs": logs}) from None
            try:
                mailbox().close()
            except Exception as e:  # noqa: BLE001
                raise ApiError(500, f"Webmail OK mais connexion IMAP impossible : {e}", {"logs": logs}) from None
            STATE.update(cookie=cookie, auto=True, imap=False, password=a.get("password") or None)
            return {"ok": True, "filters": n, "user": cfg()["user"], "logs": logs}
        cookie = (a.get("cookie") or "").strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie[7:].strip()
        if "ZM_AUTH_TOKEN=" not in cookie:
            raise ApiError(400, "Ce texte ne contient pas ZM_AUTH_TOKEN : copie bien la valeur complète de l'en-tête Cookie.")
        STATE.update(cookie=cookie, auto=False, imap=bool(STATE.get("android")) or False, password=None)
        try:
            rules = get_rules()
        except ApiError:
            STATE["cookie"] = None
            raise
        except Exception as e:  # noqa: BLE001
            STATE["cookie"] = None
            raise ApiError(502, f"Zimbra injoignable : {e}") from None
        try:
            mb = mailbox()
            mb.close()
        except SystemExit as e:
            raise ApiError(500, f"IMAP : {e}. Lance d'abord ./freemail.py auth") from None
        except Exception as e:  # noqa: BLE001
            raise ApiError(500, f"Connexion IMAP impossible : {e}. Vérifie le mot de passe avec ./freemail.py auth") from None
        return {"ok": True, "filters": len(rules), "user": cfg()["user"]}

    def api_post_logout(self, a):
        STATE.update(cookie=None, auto=False, imap=False, password=None)
        MAIL.reset()
        return {"ok": True}

    # ---- jobs
    def api_get_job(self, a):
        j = JOBS.get(a.get("id", ""))
        if not j:
            raise ApiError(404, "job inconnu")
        return j.view()

    # ---- dashboard
    def api_get_overview(self, a):
        with READ_ONLY():
            mb = mailbox()
            try:
                return {"quota": mb.quota(), "folders": mb.folder_stats(), "stats": latest_stats()}
            finally:
                mb.close()

    def api_post_report(self, a):
        def run(job):
            with READ_ONLY():
                def step(i, n, name, msgs):
                    job.set(i / n if n else 0, f"Dossier {min(int(i) + 1, n)} / {n} · {name}", f"{msgs:,} mails analysés".replace(",", " "))
                job.set(0, "Connexion à la boîte…")
                _, stats = fm.cmd_report(cfg(), argparse.Namespace(folder=None, since=None, min_newsletter=10),
                                         progress=job.log, step=step)
            return stats
        return {"job": start_job("report", run).id}

    # ---- maintenance
    def api_get_maintenance(self, a):
        return [{"key": k, "title": v["title"], "desc": v["desc"], "danger": v["danger"],
                 "needs_session": k == "replay"} for k, v in MAINT.items()]

    def api_post_maintenance_plan(self, a):
        key = a.get("key")
        if key not in MAINT:
            raise ApiError(400, "action inconnue")
        return {"job": start_job(f"plan:{key}", lambda job: maint_plan(key, job)).id}

    def api_post_maintenance_apply(self, a):
        pid = a.get("plan_id", "")
        if pid not in PLANS:
            raise ApiError(410, "Simulation expirée : relance-la.")
        if PLANS[pid]["kind"] in ("empty_trash", "empty_junk") and a.get("confirm") != "SUPPRIMER":
            raise ApiError(400, "Confirmation requise : tape SUPPRIMER.")
        return {"job": start_job("apply", lambda job: maint_apply(pid, job)).id}

    # ---- folders
    def api_get_folders(self, a):
        with READ_ONLY():
            mb = mailbox()
            try:
                return sorted(f["name"] for f in mb.selectable())
            finally:
                mb.close()

    def api_post_folders_create(self, a):
        name = (a.get("name") or "").strip().strip("/")
        if not name:
            raise ApiError(400, "nom vide")
        with IMAP_LOCK:
            mb = mailbox()
            try:
                if mb.exists(name):
                    return {"ok": True, "created": False}
                mb.create(name)
                return {"ok": True, "created": True}
            finally:
                mb.close()

    # ---- webmail
    def api_get_mail_folders(self, a):
        if a.get("vis") == "1":
            LAST_VISIBLE[0] = time.time()
        return mail(mailweb.folders)

    def api_get_mail_list(self, a):
        if a.get("body") == "1" and a.get("q"):
            return mail(lambda mb: mailweb.list_messages(mb, a.get("folder", "INBOX"), int(a.get("page", 0)), a.get("q", ""), a.get("filter", ""), body=True))
        return mail(lambda mb: mailweb.list_messages(mb, a.get("folder", "INBOX"), int(a.get("page", 0)),
                                                     a.get("q", ""), a.get("filter", "")))

    def api_get_mail_new(self, a):
        try:
            since, per = max(0.0, float(a.get("since") or 0)), min(max(int(a.get("per") or 3), 1), 200)
        except ValueError:
            raise ApiError(400, _L("Paramètres invalides", "Invalid parameters")) from None
        skip = set(extras.get_settings()["notify_off"]) | {extras.SNOOZE_FOLDER}
        return mail(lambda mb: mailweb.new_mail(mb, since, per, a.get("folder") or None, skip))

    def api_post_mail_new_read(self, a):
        names = [str(n) for n in (a.get("folders") or []) if n][:100]
        try:
            since = max(0.0, float(a.get("since") or 0))
        except (TypeError, ValueError):
            raise ApiError(400, _L("Paramètres invalides", "Invalid parameters")) from None
        res = mail(lambda mb: mailweb.mark_new_read(mb, names, since))
        for f, uids in res["marked"].items():
            seen_on_device(f, uids)
        return res

    def api_get_mail_msg(self, a):
        m = mail(lambda mb: mailweb.get_message(mb, a["folder"], a["uid"], a.get("seen", "1") == "1"))
        if a.get("seen", "1") == "1":
            seen_on_device(a["folder"], [a["uid"]])
        return m

    def api_get_mail_att(self, a):
        data, ctype, fname = mail(lambda mb: mailweb.get_attachment(mb, a["folder"], a["uid"], int(a["part"])))
        return ("__binary__", data, ctype, fname)

    def api_post_mail_flag(self, a):
        mail(lambda mb: mailweb.set_flag(mb, a["folder"], a["uids"], a["flag"], bool(a.get("on", True))))
        if a["flag"] == "seen":
            seen_on_device(a["folder"], a["uids"]) if a.get("on", True) else refresh_status()
        return {"ok": True}

    def api_post_mail_markall(self, a):
        n = mail(lambda mb: mailweb.mark_all_read(mb, a["folder"]))
        seen_on_device(a["folder"], None)
        return {"ok": True, "count": n}

    def api_post_mail_move(self, a):
        mail(lambda mb: mailweb.move(mb, a["folder"], a["uids"], a["dest"]))
        seen_on_device(a["folder"], a["uids"])
        return {"ok": True}

    def api_post_mail_delete(self, a):
        res = mail(lambda mb: mailweb.delete(mb, a["folder"], a["uids"]))
        seen_on_device(a["folder"], a["uids"])
        return {"ok": True, "result": res}

    # ---- settings, diagnostic, scheduling, snooze, contacts
    def api_get_settings(self, a):
        return extras.get_settings()

    def api_post_settings(self, a):
        s = extras.save_settings(a)
        if NOTIFIER[0]:
            NOTIFIER[0].sync_idle()
        return s

    def api_get_diag(self, a):
        import platform
        n = STATE.get("notifier") or {}
        return {"version": APP_VERSION, "platform": "android" if STATE.get("android") else sys.platform,
                "python": platform.python_version(), "user": cfg().get("user"),
                "imap": f'{cfg().get("imap_host")}:{cfg().get("imap_port")}', "zimbra": bool(STATE.get("cookie")),
                "notifier": {k: v for k, v in n.items()}, "foreground": STATE.get("foreground", True),
                "scheduled": len(extras.list_scheduled()), "snoozed": len(extras.list_snoozed()),
                "log": list(extras.LOG)[-int(a.get("n", 200)):]}

    def api_post_mail_schedule(self, a):
        data = dict(a.get("message") or {})
        data.setdefault("from_name", extras.get_settings()["from_name"])
        if data.get("include"):
            # the draft (and its parts) is deleted below: keep the attachment bytes in the outbox
            parts = mail(lambda mb: mailweb._server_parts(mb, data["include"]))
            data["attachments"] = list(data.get("attachments") or []) + [
                {"name": n, "type": t, "data": base64.b64encode(b).decode()} for b, t, n in parts]
            data["include"] = []
        r = extras.schedule_send(data, a["at"])
        if data.get("draft_uid"):
            try:
                mail(lambda mb: mailweb.delete_draft(mb, data["draft_uid"]))
            except Exception:  # noqa: BLE001
                pass
        if SCHED[0]:
            SCHED[0].kick()
        return r

    def api_get_mail_scheduled(self, a):
        return {"items": extras.list_scheduled()}

    def api_post_mail_scheduled_cancel(self, a):
        return extras.cancel_scheduled(a["id"])

    def api_post_mail_scheduled_now(self, a):
        r = extras.reschedule(a["id"], time.time())
        if SCHED[0]:
            SCHED[0].kick()
        return r

    def api_get_update(self, a):
        try:
            return extras.check_update(APP_VERSION, force=a.get("force") == "1")
        except mailweb.MailError as e:
            raise ApiError(502, str(e)) from None

    def api_post_update_skip(self, a):
        extras.save_settings({"update_skip": a.get("version", "")})
        return {"ok": True, "skipped": extras.get_settings()["update_skip"]}

    def api_post_mail_snooze(self, a):
        r = mail(lambda mb: extras.snooze(mb, a["folder"], a["uids"], a["until"]))
        seen_on_device(a["folder"], a["uids"])
        if SCHED[0]:
            SCHED[0].kick()
        return r

    def api_get_mail_snoozed(self, a):
        return {"items": extras.list_snoozed(), "folder": extras.SNOOZE_FOLDER}

    def api_get_mail_contacts(self, a):
        return {"items": mail(lambda mb: extras.contacts(mb, cfg().get("user", ""), a.get("refresh") == "1"))}

    def api_post_mail_unsubscribe(self, a):
        pw = STATE.get("password")
        if not pw:
            try:
                pw = fm.get_password(cfg()["user"])
            except SystemExit:
                pw = None
        try:
            return mail(lambda mb: mailweb.unsubscribe(cfg(), pw, mb, a["folder"], a["uid"]))
        except (mailweb.smtplib.SMTPException, OSError) as e:
            raise ApiError(502, f"Désabonnement impossible : {e}") from None

    def api_post_mail_draft(self, a):
        return mail(lambda mb: mailweb.save_draft(cfg(), mb, a))

    def api_post_mail_draft_delete(self, a):
        return mail(lambda mb: mailweb.delete_draft(mb, a.get("uid")))

    def api_get_mail_thread(self, a):
        return mail(lambda mb: mailweb.thread(mb, a["folder"], a["uid"]))

    def api_post_mail_send(self, a):
        pw = STATE.get("password")
        if not pw:
            try:
                pw = fm.get_password(cfg()["user"])
            except SystemExit as e:
                raise ApiError(400, f"Mot de passe introuvable pour l'envoi : {e}") from None
        a.setdefault("from_name", extras.get_settings()["from_name"])
        try:
            return mail(lambda mb: mailweb.send(cfg(), pw, mb, a))
        except (mailweb.smtplib.SMTPException, OSError) as e:
            raise ApiError(502, f"Envoi impossible (SMTP) : {e}") from None

    # ---- filters
    def api_get_filters(self, a):
        rules = get_rules()
        out = [rule_summary(r, i + 1) for i, r in enumerate(rules)]
        for i, r in enumerate(rules):
            tests = _tests(r)
            if not tests or not r.get("active"):
                continue
            anyof = fm._one(r.get("filterTests")).get("condition", "anyof") == "anyof"
            covered = {}
            for e in rules[:i]:
                if not e.get("active") or not _has_stop(e):
                    continue
                if fm._one(e.get("filterTests")).get("condition", "anyof") != "anyof" and len(_tests(e)) > 1:
                    continue
                for t in tests:
                    for (h2, op2, v2) in _tests(e):
                        if t[0] == h2 and op2 != "exists" and t[1] != "exists" and v2 and v2 in t[2]:
                            covered.setdefault(t, e["name"])
            if (anyof and len(covered) == len(set(tests))) or (not anyof and covered):
                out[i]["shadowed_by"] = sorted(set(covered.values()))
        return {"filters": out,
                "backups": sorted((p.name for p in fm.BACKUPS.glob("filters-*.json")), reverse=True)[:30] if fm.BACKUPS.exists() else []}

    def api_post_filters_check(self, a):
        new = model_to_rule(a.get("filter", {}))
        current = get_rules()
        with READ_ONLY():
            mb = mailbox()
            try:
                folders = {f["name"] for f in mb.folders}
                res = check_filter(new, current, a.get("position", "end"), a.get("replace"), folders)
                res["preview"] = None
                if a.get("preview"):
                    rule, note = fm.server_filter_to_rule(new, ["INBOX"])
                    if rule:
                        plan, _ = fm.plan_rules(mb, {"rules": [rule]})
                        rows = plan[0]["rows"] if plan else []
                        res["preview"] = {"count": len(rows), "samples": [row_view(r) for r in rows[:10]], "criteria": rule["_criteria"]}
                    else:
                        res["preview"] = {"count": None, "note": f"Aperçu impossible via IMAP ({note}). Le filtre fonctionnera quand même côté serveur."}
                return res
            finally:
                mb.close()

    def api_post_filters_save(self, a):
        new = model_to_rule(a.get("filter", {}))
        current = get_rules()
        replace = a.get("replace")
        with IMAP_LOCK:
            mb = mailbox()
            try:
                folders = {f["name"] for f in mb.folders}
                chk = check_filter(new, current, a.get("position", "end"), replace, folders)
                if not chk["ok"] and not a.get("force"):
                    raise ApiError(409, "Conflit bloquant : corrige-le ou force l'enregistrement.", {"issues": chk["issues"]})
                for i in chk["issues"]:
                    if i.get("create"):
                        mb.create(i["create"])
            finally:
                mb.close()
        others = [r for r in current if r.get("name") != replace]
        idx = insert_index(others, a.get("position", "end"))
        if replace and a.get("position") == "same":
            idx = next((i for i, r in enumerate(current) if r.get("name") == replace), len(others))
        result = others[:idx] + [new] + others[idx:]
        bk = set_rules(result, current, "save")
        return {"ok": True, "backup": bk, "position": idx + 1, "issues": chk["issues"]}

    def _mutate(self, fn, tag):
        current = get_rules()
        result = fn([dict(r) for r in current])
        bk = set_rules(result, current, tag)
        return {"ok": True, "backup": bk}

    def api_post_filters_toggle(self, a):
        def fn(rules):
            for r in rules:
                if r["name"] == a["name"]:
                    r["active"] = bool(a.get("active"))
            return rules
        return self._mutate(fn, "toggle")

    def api_post_filters_delete(self, a):
        return self._mutate(lambda rules: [r for r in rules if r["name"] != a["name"]], "delete")

    def api_post_filters_move(self, a):
        def fn(rules):
            i = next(i for i, r in enumerate(rules) if r["name"] == a["name"])
            j = {"up": i - 1, "down": i + 1, "top": 0, "bottom": len(rules) - 1}[a["dir"]]
            j = max(0, min(len(rules) - 1, j))
            r = rules.pop(i)
            rules.insert(j, r)
            return rules
        return self._mutate(fn, "move")

    def api_get_filters_raw(self, a):
        return next((r for r in get_rules() if r["name"] == a.get("name")), None)

    def api_post_filters_restore(self, a):
        name = str(a.get("backup", ""))
        if not re.fullmatch(r"filters-[\w.-]+\.json", name):
            raise ApiError(400, "nom de sauvegarde invalide")
        p = (fm.BACKUPS / name).resolve()
        if p.parent != fm.BACKUPS.resolve() or not p.exists():
            raise ApiError(404, "sauvegarde introuvable")
        rules = json.loads(p.read_text())
        return self._mutate(lambda _: rules, "restore")

    def api_post_filters_replay(self, a):
        """Plan replay of one filter on INBOX (apply through maintenance/apply)."""
        name = a.get("name")

        def run(job):
            job.set(0, "Lecture du filtre…")
            r = next((x for x in get_rules() if x["name"] == name), None)
            if not r:
                raise ApiError(404, "filtre introuvable")
            rule, note = fm.server_filter_to_rule(r, a.get("folders") or ["INBOX"])
            if not rule:
                return {"plan_id": None, "groups": [], "total": 0, "note": note}
            with READ_ONLY():
                mb = mailbox()
                try:
                    plan, trash = fm.plan_rules(mb, {"trash_folder": "auto", "rules": [rule]}, log=job.log, progress=job.set)
                finally:
                    mb.close()
            plan = [p for p in plan if p["rows"]]
            pid = store_plan("replay-one", {"plan": plan, "trash": trash})
            return {"plan_id": pid, "groups": plan_view(plan), "total": sum(len(p["rows"]) for p in plan)}
        return {"job": start_job("plan:replay-one", run).id}


# --------------------------------------------------------------------------- phone notifications
MUTE_ROLES = {"sent", "drafts", "trash", "junk"}


def _L(fr: str, en: str) -> str:
    """Texts generated by the server for notifications: French or English (FREEMAIL_LANG, set by the Android app)."""
    return fr if os.environ.get("FREEMAIL_LANG", "fr")[:2].lower() == "fr" else en


def notif_id(folder: str, uid) -> int:
    """Stable notification id for a message (same across process restarts)."""
    import zlib
    return zlib.crc32(f"{folder}/{uid}".encode()) & 0x7FFFFFFF


def seen_on_device(folder: str, uids):
    """A mail was read/moved/deleted in the UI: drop its phone notification and refresh the unread counter."""
    n = NOTIFIER[0]
    if not n or n.mode != "android":
        return
    try:
        from java import jclass
        b = jclass("fr.jabassou.freemail.Bridge")
        if uids is None:  # whole folder marked read
            for (f, u), nid in list(n.posted.items()):
                if f == folder:
                    b.cancelMail(int(nid))
                    n.posted.pop((f, u), None)
        else:
            for u in uids:
                b.cancelMail(int(notif_id(folder, u)))
                n.posted.pop((folder, str(u)), None)
    except Exception:  # noqa: BLE001
        pass
    refresh_status()


def refresh_status():
    if NOTIFIER[0]:
        NOTIFIER[0].kick()


class Notifier(threading.Thread):
    """Background watcher: polls folder counters over its own IMAP connection and raises
    Android notifications (Termux:API) for new unread mail, unless the UI is on screen."""

    def __init__(self, port: int, interval: int, mode: str):
        super().__init__(daemon=True, name="fm-notifier")
        self.port, self.interval, self.mode = port, max(20, interval), mode
        self.mb: fm.Mailbox | None = None
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.posted: dict = {}  # (folder, uid) -> notification id, to cancel when read in the app
        self.idlers: dict = {}
        self.state_file = fm.ROOT / "notify-state.json"
        try:  # survive process restarts: compare with the last counters seen, not with "now"
            self.prev: dict | None = json.loads(self.state_file.read_text())
        except Exception:  # noqa: BLE001
            self.prev = None
        STATE["notifier"] = {"mode": self.mode, "interval": self.interval, "last": None, "error": None, "idle": None}

    def kick(self):
        """Ask the loop thread for a check now."""
        self.wake.set()

    def check_now(self, fresh: bool = True) -> bool:
        """Synchronous check (Android alarm, IDLE push). A fresh connection avoids hanging on a socket
        that died while the phone slept. Returns True on success."""
        with self.lock:
            if fresh:
                self._drop()
            try:
                self.tick()
                STATE["notifier"].update(last=time.time(), error=None)
                return True
            except Exception as e:  # noqa: BLE001
                STATE["notifier"]["error"] = str(e)
                extras.diag_exc("mail check", e)
                self._drop()
                return False

    def _drop(self):
        try:
            if self.mb:
                self.mb.close()
        except Exception:  # noqa: BLE001
            pass
        self.mb = None

    def _conn(self) -> fm.Mailbox:
        if self.mb is None:
            self.mb = fm.Mailbox(cfg(), password=STATE.get("password"))
            self.mb.M.sock.settimeout(30)
        return self.mb

    def sync_idle(self):
        """Start/stop one IDLE watcher per push folder from the settings."""
        want = [f for f in extras.get_settings()["push_folders"]][:5]
        for f, w in list(self.idlers.items()):
            if f not in want:
                w.halt()
                self.idlers.pop(f)
        for f in want:
            if f not in self.idlers or not self.idlers[f].is_alive():
                self.idlers[f] = IdleWatcher(self, f)
                self.idlers[f].start()

    def run(self):
        self.sync_idle()
        while True:
            self.check_now(fresh=False)
            self.check_update()
            self.wake.wait(self.interval)
            self.wake.clear()

    def _status(self, now: dict):
        if self.mode != "android":
            return
        shown = [f for f in now.values() if f["role"] not in MUTE_ROLES and f["name"] != extras.SNOOZE_FOLDER and f["unseen"]]
        total = sum(f["unseen"] for f in shown)
        shown.sort(key=lambda f: (f["role"] != "inbox", -f["unseen"]))
        detail = ", ".join(f"{_L('Réception', 'Inbox') if f['role'] == 'inbox' else f['label']} {f['unseen']}" for f in shown[:4])
        try:
            from java import jclass
            jclass("fr.jabassou.freemail.Bridge").updateStatus(int(total), detail, time.strftime("%H:%M"))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _unselect(mb):
        """STATUS on the currently selected mailbox returns cached counters (RFC 3501 6.3.10):
        leave the selected state so every check sees new mail in every folder."""
        if mb.M.state == "SELECTED":
            try:
                mb.M.close()  # EXAMINE'd read-only: nothing is expunged
            except Exception:  # noqa: BLE001
                pass

    def tick(self):
        mb = self._conn()
        self._unselect(mb)
        now = {f["name"]: f for f in mailweb.folders(mb) if f["selectable"]}
        prev, self.prev = self.prev, {k: {"u": v["unseen"], "n": v.get("uidnext")} for k, v in now.items()}
        try:
            self.state_file.write_text(json.dumps(self.prev))
        except Exception:  # noqa: BLE001
            pass
        self._status(now)
        if prev is None:
            return
        muted = set(extras.get_settings()["notify_off"]) | {extras.SNOOZE_FOLDER}
        # New mail = UIDNEXT moved forward (reading or deleting other mails in between does not hide it);
        # servers without UIDNEXT fall back to the unread-count difference.
        fresh = []
        for name, f in now.items():
            p = prev.get(name)
            if p is None or f["role"] in MUTE_ROLES or name in muted:
                continue
            if isinstance(p, int):  # state file written by an older version
                p = {"u": p, "n": None}
            if f.get("uidnext") and p.get("n"):
                if f["uidnext"] > p["n"]:
                    fresh.append((f, ("uid", p["n"])))
            elif f["unseen"] > p["u"]:
                fresh.append((f, ("count", f["unseen"] - p["u"])))
        on_screen = STATE.get("foreground", True) and time.time() - LAST_VISIBLE[0] < 45
        if not fresh or on_screen:
            return
        items = []
        for f, (kind, v) in fresh:
            mb.select(mb.resolve(f["name"]))
            if kind == "uid":
                uids = [u for u in mb.search(f"UID {v}:* UNSEEN") if int(u) >= v][-5:]
            else:
                uids = mb.search("UNSEEN")[-min(v, 5):]
            if uids:
                typ, data = mb.M.uid("FETCH", ",".join(uids), mailweb._LIST_ITEMS)
                for it in mailweb._parse_list_fetch(data) if typ == "OK" else []:
                    it["folder"], it["flabel"] = f["name"], f["label"] if f["role"] != "inbox" else _L("Réception", "Inbox")
                    items.append(it)
        self._unselect(mb)
        items.sort(key=lambda x: x["date"] or "")
        self.silent = extras.in_quiet_hours()
        extras.diag(f"{len(items)} new mail(s): " + ", ".join(sorted({x['flabel'] for x in items})) + (" (quiet hours)" if self.silent else ""))
        for it in items[-4:]:
            self.notify(it)
        if len(items) > 4:
            self.notify_summary(len(items), sorted({x["flabel"] for x in items}))

    def _url(self, folder: str = "", uid: str = "") -> str:
        from urllib.parse import quote as q
        return f"http://127.0.0.1:{self.port}/" + (f"#m={q(folder, safe='')}&u={uid}" if folder else "")

    def _android(self, nid: int, title: str, body: str, folder: str = "", uid: str = ""):
        from java import jclass  # Chaquopy
        jclass("fr.jabassou.freemail.Bridge").notifyMail(int(nid), title, body, folder, str(uid), bool(getattr(self, "silent", False)))

    def _run(self, args: list[str]):
        if self.mode == "termux":
            subprocess.run(["termux-notification", *args], timeout=20, check=False)
        else:
            print("[notify]", " | ".join(args), file=sys.stderr)

    def notify(self, it: dict):
        import shlex
        frm = it["from"].get("name") or it["from"].get("email") or "?"
        body = (it.get("subject") or _L("(sans objet)", "(no subject)")) + ("\n" + it["snippet"][:160] if it.get("snippet") else "")
        flag = json.dumps({"folder": it["folder"], "uids": [it["uid"]], "flag": "seen", "on": True})
        api = f"http://127.0.0.1:{self.port}/api/mail/flag"
        nid = str(notif_id(it["folder"], it["uid"]))
        self.posted[(it["folder"], str(it["uid"]))] = int(nid)
        if self.mode == "android":
            return self._android(int(nid), f"{frm} · {it['flabel']}", body, it["folder"], it["uid"])
        mark = (f"curl -s -X POST -H {shlex.quote('X-Token: ' + TOKEN)} -H 'Content-Type: application/json' "
                f"-d {shlex.quote(flag)} {api} >/dev/null; termux-notification-remove {nid}")
        self._run(["--id", nid, "--group", "freemail", "--title", f"{frm} · {it['flabel']}", "--content", body,
                   *(["--priority", "low"] if getattr(self, "silent", False) else ["--priority", "high", "--sound", "--vibrate", "180,90,180"]), "--led-color", "7c3aed", "--icon", "mail",
                   "--action", f"termux-open-url {shlex.quote(self._url(it['folder'], it['uid']))}",
                   "--button1", "Marquer lu", "--button1-action", mark,
                   "--button2", "Ouvrir", "--button2-action", f"termux-open-url {shlex.quote(self._url(it['folder'], it['uid']))}"])

    def check_update(self):
        """Announce a new app release once (GitHub API checked every few hours, cached)."""
        r = extras.update_to_announce(APP_VERSION)
        if not r:
            return
        title = _L(f"Free Mail {r['version']} est disponible", f"Free Mail {r['version']} is available")
        body = _L("Touchez pour mettre à jour.", "Tap to update.")
        try:
            if self.mode == "android":
                from java import jclass
                jclass("fr.jabassou.freemail.Bridge").notifyUpdate(r["version"], title, body)
            elif self.mode == "termux":
                import shlex
                url = f"http://127.0.0.1:{self.port}/#update"
                self._run(["--id", "freemail-update", "--title", title, "--content", body, "--icon", "system_update",
                           "--action", f"termux-open-url {shlex.quote(url)}"])
            else:
                self._run([title])
        except Exception as e:  # noqa: BLE001
            extras.diag_exc("update notification", e)

    def notify_summary(self, n: int, folders: list[str]):
        import shlex
        if self.mode == "android":
            return self._android(1, _L(f"{n} nouveaux messages", f"{n} new messages"), _L("Dans ", "In ") + ", ".join(folders))
        self._run(["--id", "freemail-summary", "--group", "freemail", "--title", f"{n} nouveaux messages",
                   "--content", "Dans " + ", ".join(folders), "--icon", "mail",
                   "--action", f"termux-open-url {shlex.quote(self._url())}"])


NOTIFIER: list = [None]
SCHED: list = [None]


def _send_password() -> str | None:
    pw = STATE.get("password")
    if pw:
        return pw
    try:
        return fm.get_password(cfg()["user"])
    except SystemExit:
        return None


def start_server(port: int, imap_session: bool = False, notify: str = "none", interval: int = 60) -> ThreadingHTTPServer:
    """Bind the HTTP server (port 0 = any free port), open the IMAP session and start the notifier."""
    cfg()
    if imap_session:
        try:
            fm.Mailbox(cfg()).close()
            STATE["imap"] = True
            print("session IMAP ouverte (mot de passe stocké)")
        except (SystemExit, Exception) as e:  # noqa: BLE001
            print(f"[warn] session IMAP automatique impossible : {e}", file=sys.stderr)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    SCHED[0] = extras.Scheduler(cfg, _send_password, mail, refresh_status)
    SCHED[0].start()
    extras.diag(f"server started (v{APP_VERSION}, notify={notify})")
    if notify != "none":
        NOTIFIER[0] = Notifier(srv.server_address[1], interval, notify)
        NOTIFIER[0].start()
    return srv


class IdleWatcher(threading.Thread):
    """IMAP IDLE on one folder: the server pushes new mail instantly over a dedicated connection, which
    also wakes the phone from Doze (the app must be exempt from battery optimisation). One watcher per
    "push folder" (Settings, max 5); other folders are caught by the periodic check."""

    REFRESH = 540  # re-issue IDLE before NAT / server timeouts

    def __init__(self, notifier: "Notifier", folder: str = "INBOX"):
        super().__init__(daemon=True, name=f"fm-idle-{folder}")
        self.n, self.folder = notifier, folder
        self.stop = threading.Event()
        self.mb = None

    def _set(self, state):
        STATE["notifier"].setdefault("idle_folders", {})[self.folder] = state
        STATE["notifier"]["idle"] = all(v is True for v in STATE["notifier"]["idle_folders"].values())

    def _wakelock(self, ms: int):
        try:
            from java import jclass
            jclass("fr.jabassou.freemail.Bridge").holdWake(int(ms))
        except Exception:  # noqa: BLE001
            pass

    def halt(self):
        self.stop.set()
        try:
            if self.mb:
                self.mb.M.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        import select
        while not self.stop.is_set():
            mb = None
            try:
                mb = self.mb = fm.Mailbox(cfg(), password=STATE.get("password"))
                if "IDLE" not in mb.caps:
                    self._set(False)
                    extras.diag("server has no IMAP IDLE: periodic checks only", "warn")
                    return
                if not mb.exists(self.folder):
                    self._set(f"dossier introuvable : {self.folder}")
                    return
                mb.select(mb.resolve(self.folder))
                self._set(True)
                extras.diag(f"push (IDLE) on {self.folder}")
                M, sock = mb.M, mb.M.sock
                n = 0
                while not self.stop.is_set():
                    n += 1
                    tag = f"FMI{n}".encode()
                    sock.settimeout(60)
                    M.send(tag + b" IDLE\r\n")
                    if not M.readline().startswith(b"+"):
                        raise RuntimeError("IDLE refused")
                    got = False
                    deadline = time.time() + self.REFRESH
                    while time.time() < deadline and not self.stop.is_set():
                        pend = sock.pending() if hasattr(sock, "pending") else 0
                        if not pend and not select.select([sock], [], [], min(30, max(1, deadline - time.time())))[0]:
                            continue
                        line = M.readline()
                        if not line:
                            raise EOFError("IDLE connection closed")
                        if b"EXISTS" in line or b"RECENT" in line:
                            got = True
                            break
                    M.send(b"DONE\r\n")
                    while True:  # drain until the tagged completion
                        line = M.readline()
                        if not line:
                            raise EOFError("IDLE connection closed")
                        if b"EXISTS" in line or b"RECENT" in line:
                            got = True
                        if line.startswith(tag + b" "):
                            break
                    if got:
                        self._wakelock(45_000)
                        extras.diag(f"push: new mail in {self.folder}")
                        self.n.check_now(fresh=False) or self.n.check_now()
            except Exception as e:  # noqa: BLE001
                if self.stop.is_set():
                    break
                self._set(f"reconnexion ({e})")
                extras.diag(f"push {self.folder}: reconnecting ({type(e).__name__}: {e})", "warn")
                self.stop.wait(20)
            finally:
                try:
                    if mb:
                        mb.M.shutdown()
                except Exception:  # noqa: BLE001
                    pass
        STATE["notifier"].get("idle_folders", {}).pop(self.folder, None)


def _load_token(path: str | None) -> str:
    if not path:
        return TOKEN
    p = Path(path).expanduser()
    if p.is_file() and p.read_text().strip():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    p.write_text(tok)
    p.chmod(0o600)
    return tok


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description="freemail web UI (local)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--token-file", help="persist the access token (stable URL for a home-screen app)")
    ap.add_argument("--imap-session", action="store_true", help="start logged in (mailbox only, no Zimbra filters) with the stored password")
    ap.add_argument("--notify", choices=["none", "termux", "log", "android"], default="none", help="background new-mail notifications")
    ap.add_argument("--notify-interval", type=int, default=60, help="seconds between mailbox checks (min 20)")
    ap.add_argument("--termux", action="store_true", help="phone preset: --no-browser --token-file ~/.config/freemail/token --imap-session --notify termux")
    args = ap.parse_args()
    if args.termux:
        args.no_browser, args.imap_session, args.notify = True, True, "termux"
        args.token_file = args.token_file or "~/.config/freemail/token"
    TOKEN = _load_token(args.token_file)
    srv = start_server(args.port, args.imap_session, args.notify, args.notify_interval)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?t={TOKEN}"
    print(f"freemail UI : {url}\n(Ctrl+C pour arrêter - le cookie n'est gardé qu'en mémoire)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\narrêt")


if __name__ == "__main__":
    main()
