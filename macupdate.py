#!/usr/bin/env python3
"""In-app update of the macOS bundle.

The .dmg is downloaded by the app itself (a browser would add the quarantine flag that Gatekeeper blocks for an
app that is not notarized), mounted, copied, and swapped for the running bundle by a detached shell that waits for
this process to exit, then relaunches the app.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import extras

ALLOWED_PREFIX = f"https://github.com/{extras.UPDATE_REPO}/releases/download/"


def check_url(url: str) -> str:
    """Only .dmg assets of this project's releases may be installed."""
    if not (isinstance(url, str) and url.startswith(ALLOWED_PREFIX) and url.endswith(".dmg") and ".." not in url):
        raise ValueError("URL not allowed")
    return url


def current_bundle() -> Path | None:
    """The running 'Free Mail.app' (None when not launched from a bundle)."""
    for p in Path(sys.executable).resolve().parents:
        if p.suffix == ".app":
            return p
    return None


def swap_script(pid: int, new_app: Path, target: Path, workdir: Path) -> str:
    q = shlex.quote
    old = workdir / "old.app"
    return (f"while kill -0 {int(pid)} 2>/dev/null; do sleep 0.3; done\n"
            f"if mv {q(str(target))} {q(str(old))} && mv {q(str(new_app))} {q(str(target))}; then\n"
            f"  open {q(str(target))}; rm -rf {q(str(workdir))}\n"
            f"else\n  [ -d {q(str(old))} ] && mv {q(str(old))} {q(str(target))}; open {q(str(target))}\nfi\n")


def download(url: str, dest: Path, opener=urllib.request.urlopen) -> None:
    with opener(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def install(url: str, *, opener=urllib.request.urlopen, run=subprocess.run, popen=subprocess.Popen) -> dict:
    """Download, unpack and schedule the swap. The caller must exit the app right after."""
    check_url(url)
    bundle = current_bundle()
    if bundle is None:
        raise RuntimeError("Not running from a Free Mail.app bundle")
    if not os.access(bundle.parent, os.W_OK):
        raise RuntimeError(f"No write access to {bundle.parent}")
    work = Path(tempfile.mkdtemp(prefix="freemail-update-"))
    try:
        dmg, mnt, new_app = work / "update.dmg", work / "mnt", work / "new.app"
        mnt.mkdir()
        download(url, dmg, opener)
        run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify", "-mountpoint", str(mnt), str(dmg)],
            check=True, capture_output=True)
        try:
            src = next(mnt.glob("*.app"), None)
            if src is None:
                raise RuntimeError("No app found in the disk image")
            run(["ditto", str(src), str(new_app)], check=True, capture_output=True)
        finally:
            run(["hdiutil", "detach", "-force", str(mnt)], capture_output=True)
        run(["xattr", "-cr", str(new_app)], capture_output=True)  # no quarantine flag, whatever the source
        popen(["/bin/sh", "-c", swap_script(os.getpid(), new_app, bundle, work)],
              start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return {"ok": True, "target": str(bundle)}
