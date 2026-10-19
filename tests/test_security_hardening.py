"""Security hardening added with the Phase 3 integration: MFA, database-backed rate limits, encrypted attachments,
SSRF-safe SEO crawling, and session revocation."""

from __future__ import annotations

import os
import re
import time

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from dealerflow.db import session_scope
from dealerflow.models import Attachment, User
from dealerflow.security_mfa import totp
from dealerflow.web.app import create_app
from tests.conftest import login, make_user

PW = "correct horse battery"


def csrf_of(resp) -> str:
    return resp.text.split('name="csrf" value="')[1].split('"')[0]


def _enrol(browser) -> tuple[str, list[str]]:
    page = browser.get("/account/security")
    r = browser.post("/account/mfa/start", data={"csrf": csrf_of(page)})
    secret = re.search(r"Setup key: <code>([A-Z2-7]+)</code>", r.text).group(1)
    r = browser.post("/account/mfa/confirm", data={"csrf": csrf_of(r), "code": totp(secret)})
    codes = re.search(r"<pre>(.*?)</pre>", r.text, re.S).group(1).split()
    assert "enabled" in r.text and len(codes) == 8
    return secret, codes


def _password_step(browser, email: str):
    page = browser.get("/login")
    return browser.post("/login", data={"email": email, "password": PW, "csrf": csrf_of(page), "next": "/"}, follow_redirects=False)


def test_mfa_enrolment_login_replay_and_recovery(client, db):
    make_user(db, "boss@agency.example", role="admin")
    login(client, "boss@agency.example")
    secret, codes = _enrol(client)
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == "boss@agency.example"))
        assert u.mfa_enabled and secret not in (u.mfa_secret_encrypted or "")  # stored encrypted
        assert all(c not in str(u.mfa_recovery_hashes) for c in codes)  # stored hashed
    assert client.get("/settings").status_code == 200  # the enrolling session continues
    other = TestClient(create_app())
    r = _password_step(other, "boss@agency.example")
    assert r.headers["location"] == "/login/mfa"
    assert other.get("/settings", follow_redirects=False).status_code == 303  # password alone is not enough
    page = other.get("/login/mfa")
    assert "Invalid code" in other.post("/login/mfa", data={"csrf": csrf_of(page), "code": "000000"}).text
    code = totp(secret, at=time.time() + 30)  # the enrolment code (this time step) was already used: next step's code
    assert other.post("/login/mfa", data={"csrf": csrf_of(page), "code": code}, follow_redirects=False).status_code == 303
    assert other.get("/settings").status_code == 200
    third = TestClient(create_app())  # the same code cannot be replayed
    _password_step(third, "boss@agency.example")
    page = third.get("/login/mfa")
    assert "Invalid code" in third.post("/login/mfa", data={"csrf": csrf_of(page), "code": code}).text
    r = third.post("/login/mfa", data={"csrf": csrf_of(page), "code": codes[0]}, follow_redirects=False)  # recovery code
    assert r.status_code == 303
    fourth = TestClient(create_app())
    _password_step(fourth, "boss@agency.example")
    page = fourth.get("/login/mfa")
    assert "Invalid code" in fourth.post("/login/mfa", data={"csrf": csrf_of(page), "code": codes[0]}).text  # single use


def test_mfa_is_enforced_for_privileged_roles(client, db, monkeypatch):
    from dealerflow.config import get_settings

    monkeypatch.setenv("DF_MFA_ENFORCED", "true")
    get_settings.cache_clear()
    make_user(db, "mgr@agency.example", role="manager")
    make_user(db, "rep@agency.example", role="rep")
    login(client, "mgr@agency.example")
    r = client.get("/finance", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/security"
    assert client.get("/api/v1/clients", headers={"x-requested-with": "x"}).status_code in (303, 403)
    _enrol(client)
    assert client.get("/finance").status_code == 200
    rep = TestClient(create_app())
    login(rep, "rep@agency.example")  # reps are not required to enrol
    assert rep.get("/prospects").status_code == 200


def test_login_lockout_is_shared_between_processes(db):
    make_user(db, "lock@agency.example", role="rep")
    a, b = TestClient(create_app()), TestClient(create_app())  # two app instances = two web processes, one database
    for i in range(5):
        page = (a if i % 2 else b).get("/login")
        (a if i % 2 else b).post("/login", data={"email": "lock@agency.example", "password": "wrong-password",
                                                 "csrf": csrf_of(page), "next": "/"})
    page = a.get("/login")
    r = a.post("/login", data={"email": "lock@agency.example", "password": PW, "csrf": csrf_of(page), "next": "/"})
    assert "Too many failed attempts" in r.text


def test_api_rate_limit_is_shared_between_processes(db, monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.security import new_api_key

    key, digest = new_api_key()
    u = make_user(db, "api@agency.example", role="viewer")
    u.api_key_hash = digest
    db.commit()
    monkeypatch.setattr(get_settings(), "api_rate_limit_per_minute", 4)
    a, b = TestClient(create_app()), TestClient(create_app())
    codes = [(a if i % 2 else b).get("/api/v1/prospects", headers={"Authorization": f"Bearer {key}"}).status_code for i in range(6)]
    assert codes[:4] == [200] * 4 and 429 in codes[4:]


def test_receipts_are_encrypted_at_rest(client, db, admin):
    from dealerflow.crypto import MAGIC
    from dealerflow.services.maintenance import attachment_path, read_attachment

    cats = client.get("/api/v1/expense-categories", headers=_admin_key(db, admin)).json()
    csrf = login(client, admin.email)
    pdf = b"%PDF-1.4 synthetic receipt"
    client.post("/finance/expenses", data={"csrf": csrf, "expense_date": "2026-10-01", "vendor": "Host Co", "amount": "20",
                                           "category_id": str(cats[0]["id"]), "business_purpose": "Hosting"},
                files={"receipt": ("r.pdf", pdf, "application/pdf")})
    att = db.scalar(select(Attachment))
    raw = open(attachment_path(att), "rb").read()
    assert raw.startswith(MAGIC) and b"synthetic receipt" not in raw
    assert client.get(f"/finance/attachments/{att.id}").content == pdf
    # A file written before encryption existed is still readable; a tampered file is refused.
    with open(attachment_path(att), "wb") as fh:
        fh.write(pdf)
    assert read_attachment(att) == pdf
    with open(attachment_path(att), "wb") as fh:
        fh.write(b"%PDF-1.4 tampered")
    try:
        read_attachment(att)
        raise AssertionError("tampered file accepted")
    except ValueError:
        pass


def _admin_key(db, admin) -> dict:
    from dealerflow.security import new_api_key

    key, digest = new_api_key()
    db.get(User, admin.id).api_key_hash = digest
    db.commit()
    return {"Authorization": f"Bearer {key}"}


def test_seo_crawler_refuses_internal_addresses_and_redirects(monkeypatch):
    from dealerflow.services.seo_crawler import Crawler

    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.path == "/":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="<html><title>x</title></html>", headers={"content-type": "text/html"})

    mock = httpx.Client(transport=httpx.MockTransport(handler))
    crawler = Crawler("https://alpha.example", max_pages=3, delay_seconds=0.5, client=mock, sleep=lambda s: None)
    crawler.crawl()
    assert all("169.254.169.254" not in u for u in seen) and "169.254.169.254" in crawler.blocked_hosts
    internal = Crawler("http://127.0.0.1:8000", max_pages=1, delay_seconds=0.5, client=mock, sleep=lambda s: None)
    internal.crawl()
    assert not any("127.0.0.1" in u for u in seen)


def test_session_of_a_deleted_dealership_user_is_rejected(client, db):
    u = make_user(db, "gone@dealer.example", role="viewer")
    login(client, "gone@dealer.example")
    u.client_role = "deleted"
    db.commit()
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")


def test_encryption_key_is_required_in_production():
    from pydantic import SecretStr

    from dealerflow.config import Settings

    s = Settings(env="production", secret_key=SecretStr("x" * 40), base_url="https://app.example")
    assert any("DF_ENCRYPTION_KEY" in p for p in s.production_problems())
    assert os.environ.get("DF_ENCRYPTION_KEY") is None  # tests never use a real key
