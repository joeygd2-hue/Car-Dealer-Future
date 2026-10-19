"""Stage 4: server-side sessions, TOTP MFA (with replay and recovery codes), admin MFA policy and reset links."""

from __future__ import annotations

import re

from sqlalchemy import select

from tests.conftest import csrf_of, login, make_user


def _csrf(client, path="/account/security"):
    return client.get(path).text.split('name="csrf" value="')[1].split('"')[0]


def test_logout_and_end_all_revoke_sessions_on_every_device(client, db):
    from fastapi.testclient import TestClient

    from dealerflow.models import UserSession
    from dealerflow.web.app import create_app

    make_user(db, "rep@test.example", "rep")
    other = TestClient(create_app())
    login(client, "rep@test.example")
    login(other, "rep@test.example")
    assert db.scalar(select(UserSession).where(UserSession.user_id.isnot(None))) is not None
    assert other.get("/settings").status_code == 200
    r = client.post("/account/sessions/end-others", data={"csrf": _csrf(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert other.get("/settings", follow_redirects=False).headers["location"].startswith("/login")  # other device is out
    assert client.get("/settings").status_code == 200
    client.post("/logout", data={"csrf": csrf_of(client)})
    assert client.get("/settings", follow_redirects=False).status_code == 303


def test_password_change_and_admin_deactivation_end_sessions(client, db):
    from fastapi.testclient import TestClient

    from dealerflow.models import User
    from dealerflow.web.app import create_app

    admin = make_user(db, "admin@test.example", "admin")
    make_user(db, "rep@test.example", "rep")
    rep_client = TestClient(create_app())
    login(rep_client, "rep@test.example")
    login(client, "admin@test.example")
    client.post("/settings/user", data={"email": "rep@test.example", "role": "rep", "active": "0", "csrf": csrf_of(client)})
    db.expire_all()
    assert db.scalar(select(User).where(User.email == "rep@test.example")).active is False
    assert rep_client.get("/settings", follow_redirects=False).status_code == 303
    # Own password change keeps this session, ends the others.
    second = TestClient(create_app())
    login(second, "admin@test.example")
    client.post("/account/password", data={"current_password": "correct horse battery", "new_password": "a brand new passphrase",
                                           "csrf": _csrf(client)})
    assert client.get("/settings").status_code == 200
    assert second.get("/settings", follow_redirects=False).status_code == 303
    assert admin.id


def _enrol(client):
    from dealerflow import mfa

    r = client.post("/account/mfa/start", data={"csrf": _csrf(client)})
    secret = re.search(r'<p class="mono">([A-Z2-7]+)</p>', r.text).group(1)
    r = client.post("/account/mfa/confirm", data={"code": mfa.totp(secret), "csrf": _csrf(client)})
    codes = re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{8}\b", r.text)
    assert len(codes) == 8
    return secret, codes


def test_mfa_login_requires_second_factor_rejects_replay_and_accepts_recovery_code_once(client, db):
    from fastapi.testclient import TestClient

    from dealerflow import mfa
    from dealerflow.models import User
    from dealerflow.web.app import create_app

    make_user(db, "manager@test.example", "manager")
    login(client, "manager@test.example")
    secret, codes = _enrol(client)
    db.expire_all()
    u = db.scalar(select(User).where(User.email == "manager@test.example"))
    assert u.mfa_enabled and secret not in (u.mfa_secret or "") and codes[0] not in str(u.mfa_recovery_hashes)

    c2 = TestClient(create_app())
    page = c2.get("/login")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    r = c2.post("/login", data={"email": "manager@test.example", "password": "correct horse battery", "csrf": csrf, "next": "/"},
                follow_redirects=False)
    assert r.headers["location"] == "/login/mfa"
    assert c2.get("/settings", follow_redirects=False).headers["location"] == "/login/mfa"  # password alone is not enough
    mcsrf = _csrf(c2, "/login/mfa")
    assert "Invalid code" in c2.post("/login/mfa", data={"code": "000000", "csrf": mcsrf}).text
    # The code used at enrolment cannot be replayed.
    assert "Invalid code" in c2.post("/login/mfa", data={"code": mfa.totp(secret), "csrf": _csrf(c2, "/login/mfa")}).text
    r = c2.post("/login/mfa", data={"code": codes[0], "csrf": _csrf(c2, "/login/mfa")}, follow_redirects=False)
    assert r.status_code == 303 and c2.get("/settings").status_code == 200
    c3 = TestClient(create_app())
    page = c3.get("/login")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    c3.post("/login", data={"email": "manager@test.example", "password": "correct horse battery", "csrf": csrf, "next": "/"})
    assert "Invalid code" in c3.post("/login/mfa", data={"code": codes[0], "csrf": _csrf(c3, "/login/mfa")}).text  # single use


def test_admin_mfa_policy_blocks_admin_pages_until_enrolled(client, db, monkeypatch):
    from dealerflow.config import get_settings

    monkeypatch.setattr(get_settings(), "require_admin_mfa", True)
    make_user(db, "admin@test.example", "admin")
    login_page = client.get("/login")
    csrf = login_page.text.split('name="csrf" value="')[1].split('"')[0]
    client.post("/login", data={"email": "admin@test.example", "password": "correct horse battery", "csrf": csrf, "next": "/"})
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/security"
    _enrol(client)
    assert client.get("/settings").status_code == 200


def test_reset_link_is_single_use_ends_sessions_and_never_reveals_accounts(client, db):
    from fastapi.testclient import TestClient

    from dealerflow.integrations.email import MockEmailProvider
    from dealerflow.web.app import create_app

    make_user(db, "rep@test.example", "rep")
    logged_in = TestClient(create_app())
    login(logged_in, "rep@test.example")
    page = client.get("/password/forgot")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    a = client.post("/password/forgot", data={"email": "rep@test.example", "csrf": csrf}).text
    b = client.post("/password/forgot", data={"email": "nobody@test.example", "csrf": csrf}).text
    assert "If that address belongs" in a and "If that address belongs" in b
    sent = [m for m, _ in MockEmailProvider.outbox if m.to == "rep@test.example"]
    assert len(sent) == 1 and not [m for m, _ in MockEmailProvider.outbox if m.to == "nobody@test.example"]
    link = re.search(r"http://\S+/password/reset/(\S+)", sent[0].body).group(1)
    form = client.get(f"/password/reset/{link}")
    csrf = form.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post(f"/password/reset/{link}", data={"new_password": "another long passphrase", "confirm_password": "another long passphrase",
                                                     "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert logged_in.get("/settings", follow_redirects=False).status_code == 303  # every old session ended
    assert "invalid, already used" in client.get(f"/password/reset/{link}").text
    login(client, "rep@test.example", "another long passphrase")


def test_dealer_user_can_reach_account_security(client, db):
    from tests.dealer_helpers import make_client, make_dealer_user

    cid = make_client()
    make_dealer_user(cid, "owner@alpha.example")
    page = client.get("/login")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    client.post("/login", data={"email": "owner@alpha.example", "password": "correct horse battery", "csrf": csrf, "next": "/"})
    assert client.get("/account/security").status_code == 200
    assert client.get("/settings").status_code == 403
