"""Cheap checks that need no server: Python syntax and the inline script of the web UI."""
import py_compile
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["freemail.py", "mailweb.py", "webui.py", "extras.py", "android/app/src/main/python/android_bridge.py"])
def test_python_compiles(name):
    py_compile.compile(str(ROOT / name), doraise=True)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_inline_script_parses(tmp_path):
    html = (ROOT / "web/index.html").read_text()
    js = html[html.rindex("<script>") + 8: html.rindex("</script>")]
    f = tmp_path / "app.js"
    f.write_text(js)
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    sw = subprocess.run(["node", "--check", str(ROOT / "web/sw.js")], capture_output=True, text=True)
    assert sw.returncode == 0, sw.stderr


def test_every_api_route_called_by_the_ui_exists():
    import webui
    html = (ROOT / "web/index.html").read_text()
    called = set(re.findall(r"api\('([a-z/]+)", html))
    missing = [p for p in called if not any(hasattr(webui.Handler, f"api_{m}_{p.replace('/', '_')}") for m in ("get", "post"))]
    assert missing == []
