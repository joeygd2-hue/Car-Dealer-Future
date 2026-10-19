"""Stage 4 security controls not covered by test_safety_auth.py / test_safety_audit.py / test_phase4_security.py
(ported from app/tests/test_security_hardening.py). Synthetic data only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from dealerflow.config import Settings, get_settings
from dealerflow.db import utcnow
from tests.conftest import csrf_of, login, make_user
from tests.customer_helpers import dealer_login, world  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _app_client(**kw) -> TestClient:
    from dealerflow.web.app import create_app

    return TestClient(create_app(), **kw)


def _login_form(c: TestClient, email: str, password: str):
    csrf = c.get("/login").text.split('name="csrf" value="')[1].split('"')[0]
    return c.post("/login", data={"email": email, "password": password, "csrf": csrf, "next": "/"}, follow_redirects=False)


# ------------------------------------------------------------------ sessions


def test_logout_requires_csrf_and_ends_the_server_session(client, db):
    from dealerflow.models import UserSession

    user = make_user(db, "rep@test.example", "rep")
    login(client, "rep@test.example")
    old_cookie = client.cookies.get("df_session")
    assert client.post("/logout", data={}).status_code == 403
    assert client.post("/logout", data={"csrf": "forged"}).status_code == 403
    assert client.get("/settings").status_code == 200  # a refused logout leaves the session alone
    r = client.post("/logout", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    db.expire_all()
    assert db.scalar(select(UserSession).where(UserSession.user_id == user.id)) is None  # server-side row deleted
    assert client.get("/settings", follow_redirects=False).status_code == 303
    # Replaying the pre-logout cookie does not bring the session back.
    replay = _app_client()
    replay.cookies.set("df_session", old_cookie)
    r = replay.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_throttle_is_shared_across_app_instances(db):
    make_user(db, "rep@test.example", "rep")
    for _ in range(3):
        assert "Invalid email or password" in _login_form(_app_client(), "rep@test.example", "wrong-password-1").text
    second = _app_client()
    for _ in range(2):
        assert "Invalid email or password" in _login_form(second, "rep@test.example", "wrong-password-1").text
    r = _login_form(_app_client(), "rep@test.example", "correct horse battery")
    assert r.status_code == 200 and "Too many failed attempts" in r.text  # a fresh process sees the DB-backed counter


# ------------------------------------------------------------------ password reset


def test_reset_token_expires(client, db):
    from dealerflow.models import PasswordResetToken, User
    from dealerflow.security import verify_password
    from dealerflow.web.routes_auth import _valid_token, issue_reset_token

    user = make_user(db, "rep@test.example", "rep")
    link = issue_reset_token(db, user, created_by_id=None)
    db.commit()
    token = link.split("/password/reset/")[1]
    assert _valid_token(db, token) is not None
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert _valid_token(db, token) is None
    page = client.get(f"/password/reset/{token}")
    assert "invalid, already used or expired" in page.text
    csrf = client.get("/password/forgot").text.split('name="csrf" value="')[1].split('"')[0]  # same session token
    r = client.post(f"/password/reset/{token}", data={"new_password": "an expired-link passphrase", "csrf": csrf,
                                                  "confirm_password": "an expired-link passphrase"})
    assert r.status_code == 200 and "invalid, already used or expired" in r.text
    db.expire_all()
    assert verify_password("correct horse battery", db.get(User, user.id).password_hash)  # unchanged


def test_reset_token_ttl_follows_setting(db, monkeypatch):
    from dealerflow.models import PasswordResetToken
    from dealerflow.web.routes_auth import issue_reset_token

    monkeypatch.setenv("DF_PASSWORD_RESET_TTL_MINUTES", "15")
    get_settings.cache_clear()
    user = make_user(db, "rep@test.example", "rep")
    before = utcnow()
    issue_reset_token(db, user, created_by_id=None)
    db.commit()
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    assert timedelta(minutes=14) < row.expires_at - before <= timedelta(minutes=15, seconds=5)


# ------------------------------------------------------------------ data protection, headers, errors


def test_raw_meta_events_do_not_store_message_text(client, db, world, monkeypatch):
    from dealerflow.models_customer import CustomerInboundEvent
    from dealerflow.models_ops import IntegrationConnection

    monkeypatch.setenv("DF_META_APP_SECRET", "s3cret")
    get_settings.cache_clear()
    db.add(IntegrationConnection(client_id=world["a"]["id"], kind="customer_messaging", provider="meta_messenger",
                                 external_account_id="P1", status="sandbox"))
    db.commit()
    payload = {"object": "page", "entry": [{"id": "P1", "messaging": [
        {"sender": {"id": "U1"}, "timestamp": 1760000000000, "message": {"mid": "m1", "text": "my ssn 123-45-6789"}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": sig, "content-type": "application/json"})
    assert r.status_code == 200 and r.json()["handled"] == 1
    db.expire_all()
    ev = db.scalar(select(CustomerInboundEvent))
    assert ev is not None and ev.event_id == "m1"
    assert "text" not in ev.payload and "123-45-6789" not in json.dumps(ev.payload)


def test_security_headers_on_public_pages(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "same-origin" and r.headers["cache-control"] == "no-store"


def test_unhandled_error_returns_generic_500_without_details(db, world, monkeypatch):
    from dealerflow.web import routes_inbox

    c = _app_client(raise_server_exceptions=False)
    dealer_login(c, "admin@a.example")

    def boom(*a, **k):
        raise RuntimeError("secret internal detail: postgres://user:pw@db")

    monkeypatch.setattr(routes_inbox, "get_settings_row", boom)
    r = c.get(f"/dealers/{world['a']['id']}/automation")
    assert r.status_code == 500
    assert "secret internal detail" not in r.text and "postgres://" not in r.text and "Traceback" not in r.text
    assert "RuntimeError" not in r.text


# ------------------------------------------------------------------ MFA primitives


def test_totp_rfc_vectors():
    from dealerflow import mfa

    sec = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    # RFC 4226 Appendix D (HOTP, counters 0..2).
    assert [mfa.hotp(sec, i) for i in range(3)] == ["755224", "287082", "359152"]
    # RFC 6238 Appendix B (SHA-1), last 6 of the 8-digit values.
    for t, code in [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"),
                    (2000000000, "279037"), (20000000000, "353130")]:
        assert mfa.totp(sec, at=t) == code, t
    now = time.time()
    counter = mfa.verify(sec, mfa.totp(sec, at=now), last_counter=None, at=now)
    assert counter == int(now // 30)
    assert mfa.verify(sec, mfa.totp(sec, at=now), last_counter=counter, at=now) is None  # replay of a used step
    assert mfa.verify(sec, mfa.totp(sec, at=now - 300), last_counter=None, at=now) is None  # outside the window


# ------------------------------------------------------------------ configuration


def _clear_df_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("DF_"):
            monkeypatch.delenv(k)  # real env vars override the file; test the file alone


def test_env_example_loads_as_documented(monkeypatch):
    """Regression (clean-environment reproduction): `cp .env.example .env` must produce a valid config."""
    _clear_df_env(monkeypatch)
    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.env == "development" and s.database_url.startswith("sqlite:///")
    assert s.live_customer_messaging_enabled is False and s.pilot_mode is True and s.require_admin_mfa is None
    assert s.outreach_sending_enabled is False and s.client_email_sending_enabled is False and s.email_provider == "mock"
    assert s.secret_key.get_secret_value() == ""  # the inline comment is not taken as the key


def test_blank_require_admin_mfa_falls_back_to_default(tmp_path, monkeypatch):
    _clear_df_env(monkeypatch)
    f = tmp_path / ".env"
    f.write_text("DF_REQUIRE_ADMIN_MFA=\n")
    assert Settings(_env_file=str(f)).require_admin_mfa is None


def test_blank_values_fall_back_to_defaults(tmp_path, monkeypatch):
    """Old app: a blank line for a boolean/integer setting in .env means "use the default", not a startup crash."""
    _clear_df_env(monkeypatch)
    f = tmp_path / ".env"
    f.write_text("DF_REQUIRE_ADMIN_MFA=\nDF_PILOT_MODE=\nDF_SMTP_PORT=\nDF_LIVE_CUSTOMER_MESSAGING_ENABLED=\n")
    s = Settings(_env_file=str(f))
    assert s.require_admin_mfa is None and s.pilot_mode is True and s.smtp_port == 587
    assert s.live_customer_messaging_enabled is False


def _prod(**kw) -> Settings:
    base = dict(env="production", secret_key=SecretStr("k" * 40), base_url="https://app.example")
    return Settings(**{**base, **kw})


def test_production_config_fails_closed(monkeypatch):
    from dealerflow.web import app as webapp

    assert _prod().production_problems() == []
    assert _prod(require_admin_mfa=True).production_problems() == []
    weak = _prod(secret_key=SecretStr("short")).production_problems()
    assert len(weak) == 1 and "DF_SECRET_KEY" in weak[0]
    no_mfa = _prod(require_admin_mfa=False).production_problems()
    assert len(no_mfa) == 1 and "DF_REQUIRE_ADMIN_MFA" in no_mfa[0]
    insecure_live = _prod(base_url="http://app.example", live_customer_messaging_enabled=True).production_problems()
    assert len(insecure_live) == 1 and "https" in insecure_live[0] and "customer messaging" in insecure_live[0]
    assert _prod(live_customer_messaging_enabled=True).production_problems() == []
    for bad in (_prod(require_admin_mfa=False), _prod(base_url="http://app.example", live_customer_messaging_enabled=True)):
        monkeypatch.setattr(webapp, "get_settings", lambda bad=bad: bad)
        with pytest.raises(RuntimeError, match="Refusing to start"):
            webapp.create_app()
