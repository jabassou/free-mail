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


def _page(browser, server, keep_update_dialog=False, **ctx):
    page = browser.new_context(**ctx).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route(re.compile(r"^https://"), lambda r: r.abort())  # no CDN / fonts in tests
    page.goto(server.base + f"?t={TOKEN}")
    page.wait_for_selector("#mRows .mrow", timeout=20000)
    if not keep_update_dialog:  # the fake GitHub API always offers v99: close the startup prompt
        try:
            page.wait_for_selector(".upd-scrim", timeout=4000)
            page.click(".upd-scrim [data-u=later]")
        except pw.TimeoutError:
            pass
    return page, errors


def test_desktop_settings_and_compose(browser, server):
    server("settings", {"signature": "Jane T.", "undo_seconds": 5})
    page, errors = _page(browser, server, viewport={"width": 1400, "height": 900}, locale="en-US")
    page.click(".side [data-tab=settings]")
    page.wait_for_selector("#sFolders [data-sn]", state="attached")
    assert page.input_value("#sUndo") == "5"
    assert "Diagnostics" not in page.inner_text("#settings")
    assert "jabassou/free-mail" not in page.content() or page.locator("#sRepo").count() == 0
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
    for tab in ("dash", "maint", "filters", "help", "settings", "diag", "mail"):
        page.click(f".side [data-tab={tab}]")
        page.wait_for_timeout(700)
    french = re.compile(r"[àâçéèêëîïôûùüœ]|\b(le|la|les|des|du|une|pour|dans|avec|aucun|aucune|ton|tes)\b", re.I)
    missing = [s for s in page.evaluate("[...I18N_MISSING]") if french.search(s)]
    assert missing == []
    # placeholders of user-content fields (contenteditable / translate=no) are UI text too
    assert page.get_attribute("#cBody", "data-ph") == "Write your message…"
    assert page.get_attribute("#sSig", "data-ph") == "Your signature…"
    assert page.get_attribute("#sName", "placeholder") == "e.g. John Smith"
    assert errors == []


def test_update_prompt_at_startup_and_skip(browser, server):
    server("update/skip", {"version": ""})
    page, errors = _page(browser, server, keep_update_dialog=True, viewport={"width": 1400, "height": 900}, locale="en-US")
    page.wait_for_selector(".upd-scrim", timeout=8000)
    assert "99.0.0" in page.inner_text(".upd-scrim") and "New things" in page.inner_text(".upd-scrim")
    page.click(".upd-scrim [data-u=skip]")
    page.wait_for_timeout(500)
    assert server("update")["skipped"] is True
    page.reload(); page.wait_for_selector("#mRows .mrow"); page.wait_for_timeout(2500)
    assert page.locator(".upd-scrim").count() == 0, "a skipped version is not proposed again"
    page.click(".side #updBtn")                       # manual check still shows it
    page.wait_for_selector(".upd-scrim", timeout=8000)
    server("update/skip", {"version": ""})
    assert errors == []


def test_diagnostics_screen(browser, server):
    page, errors = _page(browser, server, viewport={"width": 1400, "height": 900}, locale="en-US")
    page.click(".side [data-tab=diag]")
    page.wait_for_function("document.querySelector('#sDiag').children.length > 4")
    assert "IMAP" in page.inner_text("#sDiag")
    assert errors == []


def test_phone_no_horizontal_overflow(browser, server):
    page, errors = _page(browser, server, viewport={"width": 360, "height": 780}, is_mobile=True, has_touch=True, locale="fr-FR")
    for t in ("settings", "diag", "help", "filters", "maint", "dash"):
        page.evaluate(f"tab('{t}')")
        page.wait_for_timeout(600)
        over = page.evaluate("""() => [...document.querySelectorAll('#' + document.body.dataset.tab + ' *')]
            .filter(e => e.offsetParent && e.getBoundingClientRect().right > innerWidth + 1)
            .filter(e => { for (let a = e.parentElement; a; a = a.parentElement) {   // inside a scroll box = fine
                const o = getComputedStyle(a).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden') return a.getBoundingClientRect().right > innerWidth + 1; }
                return true; })
            .map(e => (e.id || e.className || e.tagName) + ':' + Math.round(e.getBoundingClientRect().right)).slice(0, 5)""")
        assert over == [], f"{t}: {over}"
    page.evaluate("tab('mail'); openDrawer()")
    page.wait_for_timeout(400)
    page.click("#drawer #dUpd")
    page.wait_for_selector(".upd-scrim", timeout=8000)
    assert errors == []


def test_new_view_and_collapsible_folders(browser, server, imap):
    subject = imap.put(folder="Jobs/Alerts", subject="ui new view")
    page, errors = _page(browser, server, viewport={"width": 1400, "height": 900}, locale="en-US")
    page.evaluate("localStorage.setItem('fm-newp','all')")
    page.click("#mFolders [data-fold='__new__']")
    page.wait_for_selector(".ngrp")
    assert page.inner_text("#mTitle") == "New"
    assert page.locator(".ngh[data-ngt='Jobs/Alerts']").count() == 1
    # the "Folders" section folds away (and remembers it), its unread count moves to the header
    page.click("#mFolders [data-fsec]")
    assert page.locator("#mFolders [data-fold='Jobs']").count() == 0
    assert page.evaluate("JSON.parse(localStorage.getItem('fm-closed')).includes(':sec:folders')")
    page.click("#mFolders [data-fsec]")
    assert page.locator("#mFolders [data-fold='Jobs']").count() == 1
    # a mail opens in its folder (unread filter) with a way back to "New"
    page.locator(f".ngrp .mrow:has-text('{subject}')").first.click()
    page.wait_for_selector("#mBackNew.on")
    assert page.evaluate("MB.folder") == "Jobs/Alerts" and page.evaluate("MB.filter") == "unseen"
    page.click("#mBackNew")
    page.wait_for_selector(".ngrp")
    assert page.evaluate("MB.folder") == "__new__" and page.evaluate("MB.filter") == ""
    assert errors == []
