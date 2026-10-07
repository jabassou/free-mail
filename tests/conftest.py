"""Test fixtures: a real IMAPS server (Dovecot, see dovecot.sh), an SMTPS sink (aiosmtpd) and the
web UI backend (webui.py) running in-process against them."""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["FREEMAIL_INSECURE_TLS"] = "1"   # self-signed test servers only
os.environ["FREEMAIL_PASSWORD"] = "secret"
os.environ.setdefault("FREEMAIL_LANG", "en")

IMAP_PORT = int(os.environ.get("FM_IMAP_PORT", "1993"))
USER = "tester@free.fr"
TOKEN = "test-token"


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def imap_server():
    if not _port_open(IMAP_PORT):
        script = Path(__file__).with_name("dovecot.sh")
        if os.geteuid() == 0:
            subprocess.run([str(script), "start"], check=True)
        else:
            pytest.skip(f"no IMAPS server on 127.0.0.1:{IMAP_PORT} (run: sudo {script} start)")
    return IMAP_PORT


class Imap:
    """Direct IMAP access to seed and inspect the test mailbox."""

    def __init__(self, port: int):
        self.M = imaplib.IMAP4_SSL("127.0.0.1", port, ssl_context=ssl._create_unverified_context())
        self.M.login(USER, "secret")

    def ensure(self, folder: str):
        self.M.create(f'"{folder}"')

    def put(self, folder="INBOX", subject=None, body="hello", frm="Alice <alice@example.com>", seen=False, **headers):
        subject = subject or f"test {uuid.uuid4().hex[:8]}"
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = frm, USER, subject
        m["Date"] = email.utils.formatdate(localtime=True)
        m["Message-ID"] = email.utils.make_msgid(domain="example.com")
        for k, v in headers.items():
            m[k.replace("_", "-")] = v
        m.set_content(body)
        self.M.append(f'"{folder}"', r"(\Seen)" if seen else "()", imaplib.Time2Internaldate(time.time()), m.as_bytes())
        return subject

    def find(self, folder: str, subject: str) -> list[tuple[str, bool]]:
        """[(uid, seen)] of the mails with this subject."""
        typ, _ = self.M.select(f'"{folder}"')
        if typ != "OK":
            return []
        typ, d = self.M.uid("SEARCH", None, "SUBJECT", f'"{subject}"')
        out = []
        for u in (d[0] or b"").split():
            typ, f = self.M.uid("FETCH", u, "(FLAGS)")
            out.append((u.decode(), b"\\Seen" in f[0]))
        return out


@pytest.fixture(scope="session")
def imap(imap_server):
    c = Imap(imap_server)
    for f in ("Jobs", "Jobs/Alerts", "Muted"):
        c.ensure(f)
    yield c
    c.M.logout()


@pytest.fixture(scope="session")
def smtp(tmp_path_factory):
    """SMTPS sink: every accepted message is appended to smtp.messages (email.message.EmailMessage)."""
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult

    d = tmp_path_factory.mktemp("smtp")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem")], check=True, capture_output=True)
    messages: list = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            msg = email.message_from_bytes(envelope.original_content or envelope.content, policy=email.policy.default)
            msg.rcpt = envelope.rcpt_tos
            messages.append(msg)
            return "250 OK"

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "c.pem", d / "k.pem")
    port = _free_port()
    ctl = Controller(Handler(), hostname="127.0.0.1", port=port, ssl_context=ctx,
                     authenticator=lambda *a: AuthResult(success=True), auth_require_tls=False)
    ctl.start()
    ctl.messages, ctl.port = messages, port
    yield ctl
    ctl.stop()


class Api:
    def __init__(self, base: str):
        self.base = base

    def __call__(self, path: str, body=None, status=200):
        path = urllib.parse.quote(path, safe="/?&=:%")
        req = urllib.request.Request(self.base + "api/" + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                code, data = r.status, json.load(r)
        except urllib.error.HTTPError as e:
            code, data = e.code, json.load(e)
        assert code == status, f"{path}: HTTP {code} {data}"
        return data


def wait_for(fn, timeout=20.0, every=0.3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return fn()


@pytest.fixture(scope="session")
def server(imap, smtp, tmp_path_factory):
    import extras
    import webui

    home = tmp_path_factory.mktemp("home")
    webui.ROOT, webui.TOKEN = home, TOKEN
    webui.STATE["cfg"] = {"user": USER, "imap_host": "127.0.0.1", "imap_port": IMAP_PORT,
                          "smtp_host": "127.0.0.1", "smtp_port": smtp.port}
    extras.init(lambda: home)
    srv = webui.start_server(0, imap_session=True, notify="log", interval=20)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    yield Api(base)
    srv.shutdown()
