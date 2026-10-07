"""Smoke tests of the web UI in headless Chromium (desktop + phone layouts)."""
import re
import shutil

import pytest

pw = pytest.importorskip("playwright.sync_api")

from conftest import TOKEN  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"chromium not installed: {e}")
        yield b
        b.close()


def _page(browser, server, **ctx):
    page = browser.new_context(**ctx).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route(re.compile(r"^https://"), lambda r: r.abort())  # no CDN / fonts in tests
    page.goto(server.base + f"?t={TOKEN}")
    page.wait_for_selector("#mRows .mrow", timeout=20000)
    return page, errors


def test_desktop_settings_and_compose(browser, server):
    server("settings", {"signature": "Jane T.", "undo_seconds": 5})
    page, errors = _page(browser, server, viewport={"width": 1400, "height": 900}, locale="en-US")
    page.click(".side [data-tab=settings]")
    page.wait_for_selector("#sFolders [data-sn]", state="attached")
    assert page.input_value("#sUndo") == "5"
    assert "Diagnostics" in page.inner_text("#settings")
    page.click(".side [data-tab=mail]")
    page.click("#mCompose")
    assert "Jane T." in page.inner_html("#cBody")
    page.fill('[data-rcpt="to"] input', "bob@example.com,")
    page.fill("#cSubject", "ui undo test")
    page.click("#cSend")
    page.wait_for_selector(".toast .tbtn")
    page.click(".toast .tbtn")
    page.wait_for_selector("#compose.on")
    assert page.input_value("#cSubject") == "ui undo test"
    page.click("#cClose")
    assert errors == []


def test_phone_layout(browser, server):
    page, errors = _page(browser, server, viewport={"width": 412, "height": 915}, is_mobile=True, has_touch=True, locale="fr-FR")
    assert page.locator("#mRows .mwrap").count() > 0, "rows are wrapped for swipe actions on touch screens"
    page.tap("#fab")
    bar = page.evaluate("(()=>{const b=document.querySelector('#cBar');return getComputedStyle(b).flexWrap})()")
    assert bar == "nowrap"
    assert errors == []


def test_english_ui_has_no_french_left(browser, server):
    page, errors = _page(browser, server, viewport={"width": 1400, "height": 900}, locale="en-US")
    for tab in ("dash", "maint", "filters", "help", "settings", "mail"):
        page.click(f".side [data-tab={tab}]")
        page.wait_for_timeout(700)
    french = re.compile(r"[àâçéèêëîïôûùüœ]|\b(le|la|les|des|du|une|pour|dans|avec|aucun|aucune|ton|tes)\b", re.I)
    missing = [s for s in page.evaluate("[...I18N_MISSING]") if french.search(s)]
    assert missing == []
    assert errors == []
