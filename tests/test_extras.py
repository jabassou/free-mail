"""Unit tests of extras.py (no network)."""
import datetime as dt
import io
import json
import time

import pytest

import extras


@pytest.fixture()
def store(tmp_path):
    old = extras.STORE
    extras.init(lambda: tmp_path)
    yield tmp_path
    extras.STORE = old


def test_settings_defaults(store):
    s = extras.get_settings()
    assert s["push_folders"] == ["INBOX"] and s["undo_seconds"] == 8 and s["quiet"]["on"] is False


def test_settings_are_sanitized(store):
    s = extras.save_settings({
        "signature": '<b>Jane</b><script>alert(1)</script><img src="https://t.example/p.gif" onerror="x()">',
        "push_folders": [f"F{i}" for i in range(9)],
        "undo_seconds": 99,
        "quiet": {"on": True, "evil": 1},
        "unknown": "ignored",
    })
    assert "<script" not in s["signature"] and "onerror" not in s["signature"] and "<b>Jane</b>" in s["signature"]
    assert len(s["push_folders"]) == 5
    assert s["undo_seconds"] == 30
    assert s["quiet"] == {"on": True, "start": "22:00", "end": "07:00"}
    assert "unknown" not in s
    assert json.loads((store / "settings.json").read_text())["undo_seconds"] == 30


def test_push_folders_can_be_empty(store):
    assert extras.save_settings({"push_folders": []})["push_folders"] == []


@pytest.mark.parametrize("start,end,now,quiet", [
    ("22:00", "07:00", "23:30", True),
    ("22:00", "07:00", "06:59", True),
    ("22:00", "07:00", "07:00", False),
    ("22:00", "07:00", "12:00", False),
    ("12:00", "14:00", "13:00", True),
    ("12:00", "14:00", "15:00", False),
])
def test_quiet_hours(store, start, end, now, quiet):
    extras.save_settings({"quiet": {"on": True, "start": start, "end": end}})
    t = dt.datetime.combine(dt.date.today(), dt.time.fromisoformat(now))
    assert extras.in_quiet_hours(t) is quiet


def test_quiet_hours_off(store):
    extras.save_settings({"quiet": {"on": False, "start": "00:00", "end": "23:59"}})
    assert extras.in_quiet_hours() is False


def test_schedule_cancel_reschedule(store):
    r = extras.schedule_send({"to": ["bob@example.com"], "subject": "Later"}, time.time() + 3600)
    items = extras.list_scheduled()
    assert [x["id"] for x in items] == [r["id"]] and items[0]["subject"] == "Later"
    assert extras.reschedule(r["id"], time.time() + 10)["ok"]
    c = extras.cancel_scheduled(r["id"])
    assert c["ok"] and c["message"]["subject"] == "Later"
    assert extras.list_scheduled() == []
    assert extras.cancel_scheduled(r["id"])["ok"] is False


def test_schedule_in_the_past_is_refused(store):
    with pytest.raises(extras.mailweb.MailError):
        extras.schedule_send({"to": ["x@example.com"]}, time.time() - 3600)


def test_cancel_refused_while_sending(store):
    r = extras.schedule_send({"to": ["x@example.com"], "subject": "s"}, time.time() + 60)
    box = extras.STORE.load("outbox.json", [])
    box[0]["sending"] = True
    extras.STORE.save("outbox.json", box)
    assert extras.cancel_scheduled(r["id"])["ok"] is False


def test_parse_when_accepts_iso_and_epoch():
    assert extras._parse_when(1700000000) == 1700000000.0
    assert abs(extras._parse_when("2030-01-01T10:00:00+00:00") - 1893492000.0) < 1


@pytest.mark.parametrize("a,b,newer", [("v1.4.0", "1.3.2", True), ("1.10.0", "1.9.9", True), ("1.4.0", "1.4.0", False), ("v1.3", "1.4.0", False)])
def test_version_compare(a, b, newer):
    assert (extras._ver(a) > extras._ver(b)) is newer


def test_check_update(monkeypatch):
    rel = {"tag_name": "v9.0.0", "html_url": "https://github.com/o/r/releases/tag/v9.0.0", "body": "notes",
           "assets": [{"name": "FreeMail-v9.0.0.apk", "browser_download_url": "https://github.com/o/r/releases/download/v9.0.0/FreeMail-v9.0.0.apk"}]}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return Resp(json.dumps(rel).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = extras.check_update("o/r", "1.4.0")
    assert seen["url"] == "https://api.github.com/repos/o/r/releases/latest"
    assert r["available"] and r["version"] == "9.0.0" and r["apk"].endswith(".apk")
    assert extras.check_update("not a repo", "1.4.0")["configured"] is False
