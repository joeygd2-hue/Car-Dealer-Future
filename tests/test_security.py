"""Authentication, authorization, client isolation (including direct API requests), CSRF, headers, webhooks."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

import pytest

from app.integrations.stripe_webhooks import sign
from app.models.core import User
from app.security.sensitive import RedactingFilter, contains_card_number, redact


def login(client, email, password="correct-horse-battery"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def csrf_from(client, path):
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def test_login_success_sets_hardened_cookie(client, seeded):
    r = login(client, "admin@agency.example")
    assert r.status_code == 303 and r.headers["location"] == "/app"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_login_failure_and_lockout(client, seeded, db):
    for _ in range(8):
        assert login(client, "staff@agency.example", "wrong-password-xx").status_code == 401
    # Correct password is now refused because the account is locked.
    assert login(client, "staff@agency.example").status_code == 401
    u = db.query(User).filter_by(email="staff@agency.example").one()
    assert u.locked_until is not None


def test_unknown_user_same_error(client, seeded):
    r = login(client, "nobody@agency.example")
    assert r.status_code == 401 and "Invalid email or password" in r.text


def test_open_redirect_blocked(client, seeded):
    r = client.post("/login", data={"email": "admin@agency.example", "password": "correct-horse-battery", "next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/app"


def test_form_post_requires_csrf(client, seeded):
    login(client, "admin@agency.example")
    r = client.post("/app/clients", data={"name": "No CSRF Motors"}, follow_redirects=False)
    assert r.status_code == 403
    token = csrf_from(client, "/app/clients")
    r = client.post("/app/clients", data={"name": "With CSRF Motors", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303


def test_cookie_json_api_requires_csrf_header(client, seeded):
    login(client, "admin@agency.example")
    r = client.post("/api/v1/clients", json={"name": "X"})
    assert r.status_code == 403
    token = csrf_from(client, "/app/clients")
    r = client.post("/api/v1/clients", json={"name": "X"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 201


def test_logout_revokes_session(client, seeded):
    login(client, "admin@agency.example")
    token = csrf_from(client, "/app")
    client.post("/logout", data={"csrf_token": token})
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_unauthenticated_api_rejected(client, seeded):
    assert client.get("/api/v1/clients").status_code == 401
    assert client.get("/api/v1/portal/invoices").status_code == 401
    assert client.get("/api/v1/clients", headers={"Authorization": "Bearer dgs_invalid"}).status_code == 401


@pytest.mark.parametrize("path", [
    "/api/v1/clients", "/api/v1/invoices", "/api/v1/expenses", "/api/v1/finance/report", "/api/v1/agent/dashboard",
    "/api/v1/tickets", "/api/v1/audit-log", "/api/v1/costs/usage", "/api/v1/agent/prospects",
])
def test_client_users_cannot_reach_staff_api(client, h, path):
    assert client.get(path, headers=h("alpha")).status_code == 403


@pytest.mark.parametrize("path", ["/api/v1/invoices", "/api/v1/finance/report", "/api/v1/audit-log", "/api/v1/packages"])
def test_staff_cannot_reach_admin_api(client, h, path):
    assert client.get(path, headers=h("staff")).status_code == 403
    assert client.get(path, headers=h("admin")).status_code == 200


def test_staff_cannot_use_portal_api(client, h):
    assert client.get("/api/v1/portal/me", headers=h("staff")).status_code == 403


def _make_cross_client_data(client, h, seeded):
    """Invoice + finalized report + SEO issue + ticket for each dealership."""
    ids = {}
    for name in ("alpha", "bravo"):
        cid = seeded[name].id
        inv = client.post("/api/v1/invoices", headers=h("admin"), json={
            "client_id": cid, "issue_date": str(date.today()), "due_date": str(date.today() + timedelta(days=15)),
            "lines": [{"kind": "one_time", "description": f"Work for {name}", "unit_amount_cents": 10000}], "issue": True}).json()
        rep = client.post("/api/v1/reports", headers=h("staff"), json={"client_id": cid, "period_start": str(date.today().replace(day=1)), "period_end": str(date.today())}).json()
        client.post(f"/api/v1/reports/{rep['id']}/finalize", headers=h("staff"))
        t = client.post("/api/v1/tickets", headers=h("staff"), json={"client_id": cid, "kind": "client_issue", "title": f"{name} issue"}).json()
        ids[name] = {"invoice": inv["id"], "report": rep["id"], "ticket": t["id"]}
    return ids


def test_portal_isolation_lists_and_direct_ids(client, h, seeded):
    ids = _make_cross_client_data(client, h, seeded)
    inv = client.get("/api/v1/portal/invoices", headers=h("alpha")).json()
    assert [i["id"] for i in inv] == [ids["alpha"]["invoice"]]
    reps = client.get("/api/v1/portal/reports", headers=h("alpha")).json()
    assert [r["id"] for r in reps] == [ids["alpha"]["report"]]
    tix = client.get("/api/v1/portal/tickets", headers=h("alpha")).json()
    assert [t["id"] for t in tix] == [ids["alpha"]["ticket"]]
    # Direct object requests for the other dealership's records return 404 (not 403, to avoid leaking existence).
    assert client.get(f"/api/v1/portal/invoices/{ids['bravo']['invoice']}", headers=h("alpha")).status_code == 404
    assert client.get(f"/api/v1/portal/reports/{ids['bravo']['report']}", headers=h("alpha")).status_code == 404
    assert client.get(f"/api/v1/portal/reports/{ids['alpha']['report']}", headers=h("alpha")).status_code == 200
    # And the reverse direction.
    assert client.get(f"/api/v1/portal/invoices/{ids['alpha']['invoice']}", headers=h("bravo")).status_code == 404


def test_portal_html_isolation(client, h, seeded):
    ids = _make_cross_client_data(client, h, seeded)
    login(client, "manager@alpha.example")
    page = client.get("/portal").text
    assert "Alpha Motors" in page and "Bravo Auto" not in page and "bravo issue" not in page
    assert client.get(f"/portal/reports/{ids['bravo']['report']}").status_code == 404
    assert client.get("/app", follow_redirects=False).status_code == 403


def test_portal_ticket_cannot_target_other_client(client, h, seeded):
    r = client.post("/api/v1/portal/tickets", headers=h("alpha"), json={"kind": "change_request", "title": "Change", "client_id": seeded["bravo"].id})
    assert r.status_code == 201
    staff_view = client.get("/api/v1/tickets", headers=h("staff")).json()
    assert next(t for t in staff_view if t["id"] == r.json()["id"])["client_id"] == seeded["alpha"].id


def test_draft_reports_and_invoices_hidden_from_portal(client, h, seeded):
    cid = seeded["alpha"].id
    rep = client.post("/api/v1/reports", headers=h("staff"), json={"client_id": cid, "period_start": "2026-09-01", "period_end": "2026-09-30"}).json()
    inv = client.post("/api/v1/invoices", headers=h("admin"), json={"client_id": cid, "issue_date": "2026-10-01", "due_date": "2026-10-15",
                                                                  "lines": [{"kind": "one_time", "description": "Draft", "unit_amount_cents": 500}]}).json()
    assert client.get(f"/api/v1/portal/reports/{rep['id']}", headers=h("alpha")).status_code == 404
    assert client.get(f"/api/v1/portal/invoices/{inv['id']}", headers=h("alpha")).status_code == 404


def test_security_headers(client, seeded):
    r = client.get("/login")
    for hname in ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy", "x-request-id"):
        assert hname in r.headers
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_login_rate_limit(client, seeded, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 3)
    codes = [login(client, "admin@agency.example", "bad-password-000").status_code for _ in range(5)]
    assert codes[-1] == 429


def test_api_rate_limit(client, h, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "api_rate_limit_per_minute", 5)
    codes = [client.get("/api/v1/me", headers=h("staff")).status_code for _ in range(7)]
    assert 429 in codes


def test_password_policy():
    from app.security.crypto import hash_password, verify_password

    with pytest.raises(ValueError):
        hash_password("short")
    hp = hash_password("a-long-enough-password")
    assert verify_password("a-long-enough-password", hp) and not verify_password("wrong", hp)


def test_card_numbers_detected_and_rejected(client, h, seeded):
    assert contains_card_number("ref 4111 1111 1111 1111")
    assert not contains_card_number("invoice 1234567890123")  # fails Luhn
    inv = client.post("/api/v1/invoices", headers=h("admin"), json={
        "client_id": seeded["alpha"].id, "issue_date": "2026-10-01", "due_date": "2026-10-10", "issue": True,
        "lines": [{"kind": "one_time", "description": "x", "unit_amount_cents": 1000}]}).json()
    r = client.post("/api/v1/payments", headers=h("admin"), json={"client_id": seeded["alpha"].id, "invoice_id": inv["id"], "amount_cents": 1000,
                                                                "received_date": "2026-10-02", "reference": "card 4111-1111-1111-1111"})
    assert r.status_code == 400 and "card" in r.json()["detail"].lower()


def test_log_redaction():
    s = redact("password=hunter2 token: abc123 Bearer sk-ant-api03-SECRETSECRET email bob@example.com phone 281-555-0101 card 4111111111111111")
    for leaked in ("hunter2", "abc123", "SECRETSECRET", "bob@example.com", "281-555-0101", "4111111111111111"):
        assert leaked not in s
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "api_key=%s", ("sk_live_abcdefghijk",), None)
    RedactingFilter().filter(rec)
    assert "sk_live_abcdefghijk" not in rec.getMessage()


def test_stripe_webhook_rejects_bad_signature(client, seeded):
    body = json.dumps({"id": "evt_1", "type": "invoice.paid", "data": {"object": {}}}).encode()
    assert client.post("/api/v1/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=deadbeef"}).status_code == 400
    assert client.post("/api/v1/webhooks/stripe", content=body).status_code == 400
    old = sign(body, "whsec_test_secret", ts=1_000_000)  # valid HMAC but far outside tolerance
    assert client.post("/api/v1/webhooks/stripe", content=body, headers={"Stripe-Signature": old}).status_code == 400
    assert client.post("/api/v1/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(body, "whsec_test_secret")}).status_code == 200


def test_inbound_email_webhook_requires_signature(client, seeded):
    import hashlib
    import hmac

    body = json.dumps({"message_id": "<m1@x>", "from": "a@b.example", "text": "hi"}).encode()
    assert client.post("/api/v1/webhooks/inbound-email", content=body, headers={"X-Signature": "sha256=00"}).status_code == 401
    sig = "sha256=" + hmac.new(b"inbound_test_secret", body, hashlib.sha256).hexdigest()
    assert client.post("/api/v1/webhooks/inbound-email", content=body, headers={"X-Signature": sig}).json()["status"] == "accepted"
    assert client.post("/api/v1/webhooks/inbound-email", content=body, headers={"X-Signature": sig}).json()["status"] == "duplicate"


def test_production_refuses_insecure_config(monkeypatch):
    from app.config import Settings

    s = Settings(environment="production", database_url="sqlite:///x.db", base_url="http://x", secret_key="dev-insecure-secret-change-me", credentials_encryption_key="")
    problems = s.production_problems()
    assert any("SECRET_KEY" in p for p in problems) and any("CREDENTIALS_ENCRYPTION_KEY" in p for p in problems)
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: s)
    with pytest.raises(RuntimeError):
        main.create_app()


def test_credentials_encrypted_roundtrip():
    from app.security.crypto import decrypt_secret, encrypt_secret

    c = encrypt_secret("smtp-password-123")
    assert "smtp-password-123" not in c and decrypt_secret(c) == "smtp-password-123"


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz").json()
    assert r["database"] == "ok" and r["worker"] == "no recent heartbeat"


def test_attachment_type_validation(client, h, seeded):
    cats = client.get("/api/v1/expense-categories", headers=h("admin")).json()
    e = client.post("/api/v1/expenses", headers=h("admin"), json={"expense_date": "2026-10-01", "vendor": "Host Co", "category_id": cats[0]["id"],
                                                                "amount_cents": 2000, "business_purpose": "Hosting"}).json()
    bad = client.post(f"/api/v1/expenses/{e['id']}/receipt", headers=h("admin"), files={"file": ("x.pdf", b"<script>alert(1)</script>", "application/pdf")})
    assert bad.status_code == 400
    exe = client.post(f"/api/v1/expenses/{e['id']}/receipt", headers=h("admin"), files={"file": ("x.exe", b"MZ....", "application/x-msdownload")})
    assert exe.status_code == 400
    ok = client.post(f"/api/v1/expenses/{e['id']}/receipt", headers=h("admin"), files={"file": ("../../r.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert ok.status_code == 201 and ok.json()["filename"] == "r.pdf"


def test_audit_log_records_and_scrubs(client, h, seeded):
    client.post("/api/v1/clients", headers=h("staff"), json={"name": "Audited Motors"})
    rows = client.get("/api/v1/audit-log", headers=h("admin")).json()
    assert any(r["action"] == "client.created" for r in rows)
    from app.services.audit import _scrub

    assert _scrub({"smtp_password": "x", "nested": {"api_key": "y"}}) == {"smtp_password": "[REDACTED]", "nested": {"api_key": "[REDACTED]"}}
