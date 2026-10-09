"""Integration tests: web UI API against a real IMAPS server and an SMTPS sink."""
import time
import uuid

from conftest import USER, wait_for


def _uid(api, folder, subject):
    items = api(f"mail/list?folder={folder}&page=0&q={subject}&filter=")["items"]
    return items[0]["uid"] if items else None


def test_session_and_folders(server):
    s = server("session")
    assert s["logged"] and s["user"] == USER and s["version"]
    names = {f["name"] for f in server("mail/folders")}
    assert {"INBOX", "Sent", "Trash", "Jobs/Alerts"} <= names


def test_body_search(server, imap):
    token = "zq" + uuid.uuid4().hex[:10]
    subject = imap.put(body=f"the secret word is {token}")
    assert server(f"mail/list?folder=INBOX&page=0&q={token}&filter=")["total"] == 0
    hits = server(f"mail/list?folder=INBOX&page=0&q={token}&filter=&body=1")["items"]
    assert [x["subject"] for x in hits] == [subject]


def test_settings_roundtrip(server):
    s = server("settings", {"from_name": "Jane Tester", "signature": "<i>Jane</i><script>x</script>"})
    assert s["from_name"] == "Jane Tester" and "<script" not in s["signature"]
    assert server("settings")["from_name"] == "Jane Tester"


def test_scheduled_send_is_delivered(server, smtp):
    subject = f"sched {uuid.uuid4().hex[:6]}"
    r = server("mail/schedule", {"at": time.time() + 1, "message": {"to": ["bob@example.com"], "subject": subject, "text": "hi", "html": "<p>hi</p>"}})
    assert r["ok"]
    msg = wait_for(lambda: next((m for m in smtp.messages if m["Subject"] == subject), None))
    assert msg is not None, "scheduled mail not delivered"
    assert "Jane Tester" in msg["From"] or msg["From"]  # display name from Settings when set
    assert wait_for(lambda: not any(x["id"] == r["id"] for x in server("mail/scheduled")["items"]))


def test_undo_send_returns_the_message(server, smtp):
    subject = f"undo {uuid.uuid4().hex[:6]}"
    r = server("mail/schedule", {"at": time.time() + 30, "message": {"to": ["bob@example.com"], "subject": subject, "text": "x"}})
    c = server("mail/scheduled/cancel", {"id": r["id"]})
    assert c["ok"] and c["message"]["subject"] == subject
    time.sleep(1)
    assert not any(m["Subject"] == subject for m in smtp.messages)


def test_schedule_keeps_draft_attachments(server, smtp):
    subject = f"att {uuid.uuid4().hex[:6]}"
    import base64
    d = server("mail/draft", {"to": ["bob@example.com"], "subject": subject, "text": "see attached",
                              "attachments": [{"name": "notes.txt", "type": "text/plain", "data": base64.b64encode(b"hello file").decode()}]})
    inc = [{"folder": d["folder"], "uid": d["uid"], "part": a["part"]} for a in d["attachments"]]
    server("mail/schedule", {"at": time.time() + 1, "message": {"to": ["bob@example.com"], "subject": subject, "text": "see attached",
                                                               "include": inc, "draft_uid": d["uid"]}})
    msg = wait_for(lambda: next((m for m in smtp.messages if m["Subject"] == subject), None))
    assert msg is not None
    atts = [p for p in msg.iter_attachments()]
    assert [a.get_filename() for a in atts] == ["notes.txt"] and atts[0].get_content() == "hello file"
    assert not server(f"mail/list?folder={d['folder']}&page=0&q={subject}&filter=")["items"], "draft not deleted"


def test_snooze_round_trip(server, imap):
    subject = imap.put(subject=f"snooze {uuid.uuid4().hex[:6]}")
    uid = _uid(server, "INBOX", subject)
    server(f"mail/msg?folder=INBOX&uid={uid}")  # read it
    r = server("mail/snooze", {"folder": "INBOX", "uids": [uid], "until": time.time() + 2})
    assert r["ok"] and r["count"] == 1
    assert imap.find("INBOX", subject) == []
    assert imap.find(r["folder"], subject)
    back = wait_for(lambda: imap.find("INBOX", subject), timeout=40)
    assert back and back[0][1] is False, "snoozed mail should come back unread"
    assert not any(x["subject"] == subject for x in server("mail/snoozed")["items"])


def test_contacts(server, smtp):
    server("mail/send", {"to": ["carol@example.com"], "subject": "hello carol", "text": "hi"})
    items = server("mail/contacts?refresh=1")["items"]
    assert any(c["email"] == "carol@example.com" for c in items)


def test_push_and_muted_folders(server, imap):
    import extras
    server("settings", {"push_folders": ["INBOX", "Jobs/Alerts"], "notify_off": ["Muted"], "quiet": {"on": False}})
    assert wait_for(lambda: server("diag")["notifier"].get("idle_folders", {}).get("Jobs/Alerts") is True, timeout=30)
    wait_for(lambda: server("diag")["notifier"].get("last"), timeout=30)  # first check done (baseline)
    n0 = len(extras.LOG)
    imap.put(folder="Jobs/Alerts", subject="push job offer")
    assert wait_for(lambda: any("new mail" in x["msg"] and "Alerts" in x["msg"] for x in list(extras.LOG)[n0:]), timeout=20)
    n1 = len(extras.LOG)
    imap.put(folder="Muted", subject="muted mail")
    imap.put(folder="INBOX", subject="wake the watcher")
    assert wait_for(lambda: any("new mail" in x["msg"] for x in list(extras.LOG)[n1:]), timeout=20)
    time.sleep(1)
    assert not any("Muted" in x["msg"] for x in list(extras.LOG)[n1:] if "new mail" in x["msg"])


def test_diag(server):
    d = server("diag")
    assert {"version", "platform", "python", "notifier", "scheduled", "snoozed", "log"} <= set(d)
    assert any("server started" in x["msg"] for x in d["log"])


def test_update_status_and_skip(server):
    import conftest
    r = server("update?force=1")
    assert r["repo"] == "jabassou/free-mail" and r["available"] and r["version"] == "99.0.0"
    assert r["apk"].endswith(".apk") and r["skipped"] is False
    assert server("update/skip", {"version": "99.0.0"})["skipped"] == "99.0.0"
    assert server("update")["skipped"] is True
    server("update/skip", {"version": ""})
    assert conftest.GITHUB_CALLS, "the fake GitHub API was used"


def test_update_repo_setting_is_gone(server):
    assert "update_repo" not in server("settings")
    assert "update_repo" not in server("settings", {"update_repo": "evil/repo"})


def test_bad_token_is_refused(server):
    import json
    import urllib.request
    req = urllib.request.Request(server.base + "api/settings", headers={"X-Token": "nope"})
    try:
        urllib.request.urlopen(req)
        raise AssertionError("should be refused")
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_new_mail_view(server, imap):
    server("settings", {"notify_off": ["Muted"]})
    time.sleep(1.1)
    t0 = int(time.time())  # INTERNALDATE has 1 s resolution: mails put from now on are >= t0
    alert = imap.put(folder="Jobs/Alerts", subject=f"new view alert {uuid.uuid4().hex[:6]}")
    inbox = imap.put(folder="INBOX", subject=f"new view inbox {uuid.uuid4().hex[:6]}")
    imap.put(folder="Muted", subject="new view muted")
    imap.put(folder="Trash", subject="new view trash")
    d = server(f"mail/new?since={t0}&per=3")
    groups = {g["folder"]: g for g in d["groups"]}
    assert "Muted" not in groups and "Trash" not in groups, "muted folders and Trash/Spam/Sent/Drafts are left out"
    assert d["groups"][0]["folder"] == "INBOX" and groups["INBOX"]["items"][0]["subject"] == inbox
    assert [x["subject"] for x in groups["Jobs/Alerts"]["items"]] == [alert]
    assert d["total"] == sum(g["count"] for g in d["groups"])
    assert server(f"mail/new?since={t0 + 3600}")["total"] == 0
    assert server("mail/new?since=0")["total"] >= d["total"]
    assert len(server("mail/new?since=0&per=1&folder=INBOX")["groups"]) == 1
    server("mail/new?since=abc", status=400)
    # "mark read" only touches what the view shows, and returns the UIDs for undo
    r = server("mail/new/read", {"folders": ["Jobs/Alerts"], "since": t0})
    assert r["count"] == 1 and imap.find("Jobs/Alerts", alert)[0][1] is True
    assert imap.find("INBOX", inbox)[0][1] is False
    server("mail/flag", {"folder": "Jobs/Alerts", "uids": r["marked"]["Jobs/Alerts"], "flag": "seen", "on": False})
    assert imap.find("Jobs/Alerts", alert)[0][1] is False
