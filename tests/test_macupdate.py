"""macupdate.py: in-app update of the macOS bundle (no network, no hdiutil, no real swap)."""
from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import macupdate  # noqa: E402

DMG = "https://github.com/jabassou/free-mail/releases/download/v1.12.0/FreeMail-v1.12.0-macos-arm64.dmg"


def test_check_url_only_allows_project_dmg_assets():
    assert macupdate.check_url(DMG) == DMG
    for bad in ["https://evil.example/FreeMail.dmg", DMG.replace(".dmg", ".zip"), "http://github.com/jabassou/free-mail/releases/download/v1/a.dmg",
                "https://github.com/other/repo/releases/download/v1/a.dmg", DMG + "/../x.dmg", "", None]:
        with pytest.raises(ValueError):
            macupdate.check_url(bad)


def test_current_bundle_is_found_from_the_executable(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/Applications/Free Mail.app/Contents/MacOS/Free Mail")
    assert macupdate.current_bundle().name == "Free Mail.app"
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    assert macupdate.current_bundle() is None


def test_swap_script_waits_for_exit_swaps_and_relaunches():
    s = macupdate.swap_script(4242, Path("/tmp/w x/new.app"), Path("/Applications/Free Mail.app"), Path("/tmp/w x"))
    assert "kill -0 4242" in s and "'/Applications/Free Mail.app'" in s and "'/tmp/w x/new.app'" in s
    assert s.index("kill -0") < s.index("mv ") < s.index("open ")
    assert "old.app" in s  # the previous bundle is put back if the swap fails


def test_install_downloads_unpacks_and_schedules_the_swap(tmp_path, monkeypatch):
    bundle = tmp_path / "apps" / "Free Mail.app"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(macupdate, "current_bundle", lambda: bundle)
    calls, spawned = [], []

    def opener(url, timeout=0):
        assert url == DMG
        return io.BytesIO(b"fake dmg")

    class Ok:
        returncode = 0

    def run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "hdiutil" and cmd[1] == "attach":
            (Path(cmd[cmd.index("-mountpoint") + 1]) / "Free Mail.app").mkdir()
        if cmd[0] == "ditto":
            Path(cmd[2]).mkdir()
        return Ok()

    r = macupdate.install(DMG, opener=opener, run=run, popen=lambda cmd, **kw: spawned.append(cmd))
    assert r["ok"] and r["target"] == str(bundle)
    assert calls == ["hdiutil", "ditto", "hdiutil", "xattr"]
    assert len(spawned) == 1 and spawned[0][:2] == ["/bin/sh", "-c"] and str(bundle) in spawned[0][2]
    work = Path(spawned[0][2].split("rm -rf ")[1].split("\n")[0].strip("'"))
    assert (work / "update.dmg").read_bytes() == b"fake dmg"
    shutil.rmtree(work, ignore_errors=True)


def test_install_refuses_outside_a_bundle_and_bad_urls(monkeypatch):
    with pytest.raises(ValueError):
        macupdate.install("https://evil.example/x.dmg")
    monkeypatch.setattr(macupdate, "current_bundle", lambda: None)
    with pytest.raises(RuntimeError):
        macupdate.install(DMG)
