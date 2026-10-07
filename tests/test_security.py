"""Regression tests for the code-scanning fixes."""
import urllib.error
import urllib.request

import mailweb
from conftest import TOKEN


def _get(server, path):
    req = urllib.request.Request(server.base + path.lstrip("/"), headers={"X-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def test_static_whitelist(server):
    code, body = _get(server, "/vendor/chart.umd.min.js")
    assert code == 200 and b"Chart.js v4" in body[:200]
    for path in ("/webui.py", "/vendor/../../webui.py", "/icons/../index.html", "/%2e%2e/config.yaml"):
        assert _get(server, path)[0] in (403, 404), path


def test_backup_restore_rejects_other_files(server):
    for name in ("../config.yaml", "/etc/passwd", "filters-x.json/../../a.json", "notes.txt"):
        r = server("filters/restore", {"backup": name}, status=400)
        assert r["error"]


def test_script_tags_are_stripped():
    for html in ("a<script>x</script>b", "a<script>x</script\t\n bar>b", "a<SCRIPT src=x>never closed"):
        out = mailweb._clean_html(html)
        assert "<script" not in out.lower() and "x</" not in out
