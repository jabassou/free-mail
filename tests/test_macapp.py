"""macapp.py: data directory and first-launch credentials (no window, no osascript, no network)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import freemail as fm  # noqa: E402
import macapp  # noqa: E402


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw


@pytest.fixture
def kr(monkeypatch):
    import keyring
    k = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", k.get_password)
    monkeypatch.setattr(keyring, "set_password", k.set_password)
    return k


def answers(*vals):
    it = iter(vals)
    return lambda *a, **k: next(it)


def test_prepare_home_seeds_once(tmp_path):
    bundle, home = tmp_path / "b", tmp_path / "home"
    bundle.mkdir()
    (bundle / "rules.example.yaml").write_text("rules: []\n")
    macapp.prepare_home(home, bundle)
    assert (home / "rules.yaml").read_text() == "rules: []\n"
    (home / "rules.yaml").write_text("rules: [mine]\n")
    macapp.prepare_home(home, bundle)  # never overwritten
    assert (home / "rules.yaml").read_text() == "rules: [mine]\n"


def test_first_launch_stores_password_after_a_refused_login(tmp_path, kr, monkeypatch):
    calls, alerts = [], []

    class Box:
        def __init__(self, cfg, password=None):
            calls.append((cfg["user"], password))
            if password == "bad":
                raise RuntimeError("AUTHENTICATIONFAILED")

        def close(self):
            pass

    monkeypatch.setattr(fm, "Mailbox", Box)
    ask = answers("me@free.fr", "bad", "me@free.fr", "good")
    assert macapp.ensure_credentials(tmp_path, fm, ask=ask, alert=alerts.append) == "me@free.fr"
    assert calls == [("me@free.fr", "bad"), ("me@free.fr", "good")] and len(alerts) == 1
    assert kr.store == {(fm.KEYRING_SERVICE, "me@free.fr"): "good"}  # only the working password is stored
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["user"] == "me@free.fr"


def test_already_configured_does_not_ask(tmp_path, kr):
    (tmp_path / "config.yaml").write_text("user: me@free.fr\n")
    kr.set_password(fm.KEYRING_SERVICE, "me@free.fr", "pw")
    def boom(*a, **k):
        raise AssertionError("must not prompt")
    assert macapp.ensure_credentials(tmp_path, fm, ask=boom, alert=boom) == "me@free.fr"


def test_cancel_returns_none(tmp_path, kr):
    assert macapp.ensure_credentials(tmp_path, fm, ask=answers(None), alert=lambda m: None) is None
    assert macapp.ensure_credentials(tmp_path, fm, ask=answers("me@free.fr", None), alert=lambda m: None) is None
    assert kr.store == {}


def test_repoint_moves_data_but_not_the_web_files(tmp_path):
    import webui
    old = (fm.ROOT, fm.OUT, fm.BACKUPS, fm.BROWSER_MODE_FILE, webui.ROOT, webui.WEB)
    cwd, cfg = Path.cwd(), webui.STATE["cfg"]
    try:
        macapp.repoint(fm, webui, tmp_path)
        assert fm.OUT == tmp_path / "out" and fm.BACKUPS == tmp_path / "backups" and webui.ROOT == tmp_path
        assert webui.WEB == old[5]
    finally:
        fm.ROOT, fm.OUT, fm.BACKUPS, fm.BROWSER_MODE_FILE, webui.ROOT, webui.WEB = old
        webui.STATE["cfg"] = cfg  # the shared test server keeps using its own config
        import os
        os.chdir(cwd)


def test_update_check_picks_the_dmg_for_this_arch(monkeypatch):
    import extras
    import platform
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    d = {"FreeMail-v1-macos-x86_64.dmg": "u-intel", "FreeMail-v1-macos-arm64.dmg": "u-arm"}
    assert extras._pick_dmg(d) == "u-arm"
    assert extras._pick_dmg({"x.dmg": "only"}) == "only" and extras._pick_dmg({}) is None
