"""Phase 3.5 security audit: each test pins down a control that was missing or weak before."""

import time
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app import db as dbmod
from app.config import ConfigError, Settings, get_settings, validate_settings
from app.db import utcnow
from app.models import AuditLog, InboundEvent, ListingDraft, Notification, PasswordResetToken, User, UserSession
from app.models.core import AuditLogImmutableError
from app.security import mfa
from app.services import audit
from tests.conftest import login, run_worker

# ------------------------------------------------------------------ configuration / secrets


def test_config_validation_fails_closed():
    with pytest.raises(ConfigError, match="ENCRYPTION_KEY is not a valid"):
        validate_settings(Settings(encryption_key="not-a-key"))
    with pytest.raises(ConfigError, match="required in production"):
        validate_settings(Settings(app_env="production", encryption_key=None, base_url="https://x.example"))
    with pytest.raises(ConfigError, match="https"):
        validate_settings(Settings(app_env="production", encryption_key=get_settings().encryption_key,
                                   base_url="http://x.example"))
    with pytest.raises(ConfigError, match="APP_ENV"):
        validate_settings(Settings(app_env="prod"))
    warnings = validate_settings(Settings(live_integrations_enabled=True, pilot_mode=False))
    assert any("NOT restricted" in w for w in warnings)


# ------------------------------------------------------------------ audit integrity


def test_audit_log_is_append_only_and_tamper_evident(db, world):
    audit.record(db, "test.one", actor_type="SYSTEM", foo="bar")
    audit.record(db, "test.two", actor_type="SYSTEM")
    db.commit()
    assert audit.verify_integrity(db)["ok"]
    row = db.scalar(select(AuditLog).where(AuditLog.action == "test.one"))
    row.action = "edited"
    with pytest.raises(AuditLogImmutableError):
        db.commit()
    db.rollback()
    with pytest.raises(AuditLogImmutableError):
        db.delete(db.scalar(select(AuditLog).where(AuditLog.action == "test.two")))
        db.commit()
    db.rollback()
    # A raw SQL edit (bypassing the ORM) is detected by the HMAC check …
    db.execute(text("UPDATE audit_logs SET outcome='OK', details='{\"foo\": \"tampered\"}' WHERE action='test.one'"))
    db.commit()
    report = audit.verify_integrity(db)
    assert not report["ok"] and row.id in report["tampered_ids"]
    # … and a raw delete shows up as a gap.
    middle = db.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).offset(1).limit(1))
    db.execute(text(f"DELETE FROM audit_logs WHERE id={middle}"))
    db.commit()
    assert audit.verify_integrity(db)["missing_id_ranges"]


# ------------------------------------------------------------------ sessions, CSRF, MFA


def test_logout_requires_csrf_and_ends_session(client, world):
    csrf = login(client, "staff@a.example")
    assert client.post("/logout", data={}).status_code == 403
    assert client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert client.get(f"/portal/{world['a']['d'].id}", follow_redirects=False).status_code == 303


def test_login_throttle_is_shared_across_app_instances(world):
    from fastapi.testclient import TestClient

    from app.main import create_app

    for _ in range(10):
        TestClient(create_app()).post("/login", data={"email": "staff@a.example", "password": "wrong-wrong-1"})
    r = TestClient(create_app()).post("/login", data={"email": "staff@a.example", "password": "correct-horse-battery"})
    assert "Too many attempts" in r.text  # a fresh process sees the same DB-backed counter


def _enable_mfa(user_id: int) -> str:
    from app.security.crypto import encrypt_json

    secret = mfa.new_secret()
    _, hashes = mfa.new_recovery_codes()
    with dbmod.SessionLocal() as s:
        u = s.get(User, user_id)
        u.mfa_enabled, u.mfa_secret_encrypted, u.mfa_recovery_hashes = True, encrypt_json({"secret": secret}), hashes
        s.commit()
    return secret


def test_mfa_login_flow_and_replay_protection(client, world):
    secret = _enable_mfa(world["a"]["admin"].id)
    r = client.post("/login", data={"email": "admin@a.example", "password": "correct-horse-battery"}, follow_redirects=False)
    assert r.headers["location"] == "/login/mfa"
    # Password alone grants nothing.
    assert client.get(f"/portal/{world['a']['d'].id}", follow_redirects=False).headers["location"] == "/login/mfa"
    assert "Invalid code" in client.post("/login/mfa", data={"code": "000000"}).text
    code = mfa.totp(secret)
    r = client.post("/login/mfa", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portal"
    assert client.get(f"/portal/{world['a']['d'].id}").status_code == 200
    # Same code cannot be replayed in a second login.
    from fastapi.testclient import TestClient

    from app.main import create_app

    c2 = TestClient(create_app())
    c2.post("/login", data={"email": "admin@a.example", "password": "correct-horse-battery"})
    assert "Invalid code" in c2.post("/login/mfa", data={"code": code}).text


def test_mfa_enrollment_via_ui(client, world):
    csrf = login(client, "staff@a.example")
    page = client.post("/account/mfa/start", data={"csrf_token": csrf}).text
    import re

    secret = re.search(r'<p class="mono">([A-Z2-7]+)</p>', page).group(1)
    page = client.post("/account/mfa/confirm", data={"csrf_token": csrf, "code": mfa.totp(secret)}).text
    assert "Save your recovery codes" in page
    with dbmod.SessionLocal() as s:
        u = s.scalar(select(User).where(User.email == "staff@a.example"))
        assert u.mfa_enabled and len(u.mfa_recovery_hashes) == 8 and secret not in (u.mfa_secret_encrypted or "")


def test_admin_mfa_enforced_when_required(client, world, monkeypatch):
    monkeypatch.setenv("REQUIRE_ADMIN_MFA", "true")
    get_settings.cache_clear()
    try:
        login(client, "ops@platform.example")
        r = client.get("/admin", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/account/security"
        assert client.get("/account/security").status_code == 200
    finally:
        get_settings.cache_clear()


def test_recovery_code_single_use():
    codes, hashes = mfa.new_recovery_codes(2)
    remaining = mfa.use_recovery_code(hashes, codes[0])
    assert remaining is not None and len(remaining) == 1
    assert mfa.use_recovery_code(remaining, codes[0]) is None


# ------------------------------------------------------------------ password reset


def test_password_reset_flow(client, world):
    staff = world["a"]["staff"]
    c_logged = login(client, "staff@a.example")
    assert c_logged
    from fastapi.testclient import TestClient

    from app.main import create_app

    anon = TestClient(create_app())
    same1 = anon.post("/password/forgot", data={"email": "staff@a.example"}).text
    same2 = anon.post("/password/forgot", data={"email": "nobody@nowhere.example"}).text
    assert "If that email belongs" in same1 and "If that email belongs" in same2  # no enumeration
    run_worker()
    with dbmod.SessionLocal() as s:
        n = s.scalar(select(Notification).where(Notification.subject == "Reset your password"))
        assert n.status == "SIMULATED"  # sandbox: not actually emailed
        token = n.body.split("/password/reset/")[1].split()[0]
        assert s.scalar(select(PasswordResetToken)).token_hash != token  # only the hash is stored
    r = anon.post(f"/password/reset/{token}", data={"new_password": "brand-new-password-1",
                                                     "confirm_password": "brand-new-password-1"}, follow_redirects=False)
    assert r.status_code == 303
    with dbmod.SessionLocal() as s:
        assert s.scalar(select(UserSession).where(UserSession.user_id == staff.id)) is None  # all sessions ended
    assert client.get(f"/portal/{world['a']['d'].id}", follow_redirects=False).status_code == 303
    assert "invalid, already used, or expired" in anon.get(f"/password/reset/{token}").text  # single use
    login(anon, "staff@a.example", "brand-new-password-1")


def test_reset_token_expires(db, world):
    from app.web.auth_routes import _valid_token, issue_reset_token

    token = issue_reset_token(db, db.get(User, world["a"]["staff"].id), created_by_id=None)
    db.commit()
    assert _valid_token(db, token) is not None
    row = db.scalar(select(PasswordResetToken))
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert _valid_token(db, token) is None


def test_admin_new_member_gets_setup_link_not_password_in_url(client, world):
    csrf = login(client, "ops@platform.example")
    r = client.post(f"/admin/dealerships/{world['a']['d'].id}/members",
                    data={"email": "new@a.example", "name": "New Person", "role": "DEALER_STAFF", "csrf_token": csrf},
                    follow_redirects=False)
    assert r.status_code == 200 and "/password/reset/" in r.text and "location" not in r.headers
    with dbmod.SessionLocal() as s:
        assert s.scalar(select(PasswordResetToken)).purpose == "SETUP"


def test_deactivation_ends_sessions(client, world):
    login(client, "staff@a.example")
    from fastapi.testclient import TestClient

    from app.main import create_app

    admin = TestClient(create_app())
    csrf = login(admin, "ops@platform.example")
    admin.post(f"/admin/users/{world['a']['staff'].id}/deactivate", data={"csrf_token": csrf})
    assert client.get(f"/portal/{world['a']['d'].id}", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ roles, data protection, errors


def test_only_dealer_admin_can_approve_listings(client, world, db):
    ld = ListingDraft(dealership_id=world["a"]["d"].id, vehicle_id=world["a"]["civic"].id, title="t", body="b")
    db.add(ld)
    db.commit()
    csrf = login(client, "staff@a.example")
    assert client.post(f"/portal/{world['a']['d'].id}/listings/{ld.id}/approve", data={"csrf_token": csrf}).status_code == 403


def test_raw_meta_events_do_not_store_message_text(db, world, client, monkeypatch):
    import hashlib
    import hmac
    import json

    from app.models import IntegrationConnection

    monkeypatch.setenv("META_APP_SECRET", "s3cret")
    get_settings.cache_clear()
    db.add(IntegrationConnection(dealership_id=world["a"]["d"].id, provider="META_MESSENGER", external_account_id="P1"))
    db.commit()
    payload = {"object": "page", "entry": [{"id": "P1", "messaging": [{"sender": {"id": "U1"}, "timestamp": 1,
                                                                       "message": {"mid": "m1", "text": "my ssn 123-45-6789"}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": sig}).status_code == 200
    ev = db.scalar(select(InboundEvent))
    assert "text" not in ev.payload and "123-45-6789" not in json.dumps(ev.payload)
    get_settings.cache_clear()


def test_security_headers_and_generic_errors(world, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.web import portal

    c = TestClient(create_app(), raise_server_exceptions=False)
    r = c.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    login(c, "staff@a.example")

    def boom(*a, **k):
        raise RuntimeError("secret internal detail: postgres://user:pw@db")

    monkeypatch.setattr(portal, "get_settings_row", boom)
    r = c.get(f"/portal/{world['a']['d'].id}")
    assert r.status_code == 500 and "secret internal detail" not in r.text and "Reference" in r.text


def test_totp_rfc_vector():
    import base64

    sec = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert mfa.hotp(sec, 1) == "287082"  # RFC 4226 Appendix D, counter 1
    assert mfa.verify(sec, mfa.totp(sec, at=time.time()), last_counter=None) is not None
