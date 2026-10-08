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


class FakeGitHub:
    def __init__(self, tag="v9.0.0"):
        self.tag, self.calls = tag, []

    def __call__(self, url, headers):
        self.calls.append(dict(headers))
        etag = '"' + self.tag + '"'
        if headers.get("If-None-Match") == etag:
            return 304, {"ETag": etag}, b""
        body = {"tag_name": self.tag, "html_url": "https://github.com/jabassou/free-mail/releases/x", "body": "notes",
                "assets": [{"name": "FreeMail.apk", "browser_download_url": "https://github.com/x/FreeMail.apk"}]}
        return 200, {"ETag": etag}, json.dumps(body).encode()


def test_check_update_uses_the_app_repository(store, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(extras, "_http_get", lambda url, h: (gh.calls.append(url) or gh(url, h)))
    r = extras.check_update("1.4.0")
    assert r["available"] and r["version"] == "9.0.0" and r["apk"].endswith(".apk")
    assert "https://api.github.com/repos/jabassou/free-mail/releases/latest" in gh.calls


def test_check_update_is_cached_and_uses_etag(store, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(extras, "_http_get", gh)
    extras.check_update("1.4.0")
    extras.check_update("1.4.0")                      # cached: no request
    assert len(gh.calls) == 1
    extras.check_update("1.4.0", force=True)         # forced: conditional request, answered 304
    assert len(gh.calls) == 2 and gh.calls[1].get("If-None-Match") == '"v9.0.0"'
    assert extras.check_update("1.4.0")["version"] == "9.0.0"


def test_up_to_date_and_older_release(store, monkeypatch):
    monkeypatch.setattr(extras, "_http_get", FakeGitHub("v1.4.0"))
    assert extras.check_update("1.4.0")["available"] is False
    assert extras.check_update("1.5.0", force=True)["available"] is False


def test_update_announced_once_and_skippable(store, monkeypatch):
    gh = FakeGitHub("v9.0.0")
    monkeypatch.setattr(extras, "_http_get", gh)
    assert extras.update_to_announce("1.4.0")["version"] == "9.0.0"
    assert extras.update_to_announce("1.4.0") is None           # same version: announced once
    gh.tag = "v9.1.0"
    extras.save_settings({"update_skip": "9.1.0"})
    extras.check_update("1.4.0", force=True)
    assert extras.update_to_announce("1.4.0") is None           # skipped by the user
    gh.tag = "v9.2.0"
    extras.check_update("1.4.0", force=True)
    assert extras.update_to_announce("1.4.0")["version"] == "9.2.0"


def test_update_check_offline(store, monkeypatch):
    def boom(url, h):
        raise OSError("network unreachable")
    monkeypatch.setattr(extras, "_http_get", boom)
    with pytest.raises(extras.mailweb.MailError):
        extras.check_update("1.4.0")
    assert extras.update_to_announce("1.4.0") is None
