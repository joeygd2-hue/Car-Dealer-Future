"""Phase 4 security review: client isolation (incl. direct API requests), roles, CSRF, headers, webhooks, uploads."""

from __future__ import annotations

import json
from datetime import date

import pytest

from dealerflow.integrations.stripe import sign
from dealerflow.sensitive import RedactingFilter, contains_card_number, redact
from tests.conftest import login, make_user
from tests.phase4 import issued_invoice, portal_login, two_clients


@pytest.fixture
def t(db):
    return two_clients(db)


def _cross_data(client, t):
    ids = {}
    for name in ("alpha", "bravo"):
        cid = t[name].id
        inv = issued_invoice(client, t["h"]["admin"], cid)
        rep = client.post("/api/v1/client-reports", headers=t["h"]["rep"], json={"client_id": cid, "period_start": str(date.today().replace(day=1)),
                                                                                "period_end": str(date.today())}).json()
        client.post(f"/api/v1/client-reports/{rep['id']}/publish", headers=t["h"]["rep"])
        tk = client.post("/api/v1/tickets", headers=t["h"]["rep"], json={"client_id": cid, "kind": "client_issue", "title": f"{name} issue"}).json()
        ids[name] = {"invoice": inv["id"], "report": rep["id"], "ticket": tk["id"]}
    return ids


def test_portal_lists_and_direct_ids_are_isolated(client, t):
    ids = _cross_data(client, t)
    ha, hb = t["h"]["alpha"], t["h"]["bravo"]
    assert [i["id"] for i in client.get("/api/v1/portal/invoices", headers=ha).json()] == [ids["alpha"]["invoice"]]
    assert [r["id"] for r in client.get("/api/v1/portal/reports", headers=ha).json()] == [ids["alpha"]["report"]]
    assert [x["id"] for x in client.get("/api/v1/portal/tickets", headers=ha).json()] == [ids["alpha"]["ticket"]]
    # Direct requests for the other dealership's records: 404, in both directions.
    assert client.get(f"/api/v1/portal/invoices/{ids['bravo']['invoice']}", headers=ha).status_code == 404
    assert client.get(f"/api/v1/portal/reports/{ids['bravo']['report']}", headers=ha).status_code == 404
    assert client.get(f"/api/v1/portal/invoices/{ids['alpha']['invoice']}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/portal/reports/{ids['alpha']['report']}", headers=ha).status_code == 200


@pytest.mark.parametrize("path", [
    "/api/v1/clients", "/api/v1/invoices", "/api/v1/finance/report", "/api/v1/tickets", "/api/v1/costs/usage", "/api/v1/prospects",
    "/api/v1/reports/weekly", "/api/v1/seo/issues?client_id=1", "/", "/prospects", "/agent", "/settings", "/finance", "/clients", "/audit",
])
def test_client_users_are_confined_to_the_portal(client, t, path):
    assert client.get(path, headers=t["h"]["alpha"]).status_code == 403


def test_client_portal_html_isolation(client, t):
    ids = _cross_data(client, t)
    portal_login(client, "alpha@p4.example")
    page = client.get("/portal").text
    assert "Alpha Motors" in page and "Bravo Auto" not in page and "bravo issue" not in page
    assert client.get(f"/portal/reports/{ids['bravo']['report']}").status_code == 404
    assert client.get("/prospects", follow_redirects=False).status_code == 403


def test_portal_ticket_is_bound_to_own_client(client, t):
    r = client.post("/api/v1/portal/tickets", headers=t["h"]["alpha"], json={"kind": "change_request", "title": "Hours", "client_id": t["bravo"].id})
    assert r.status_code == 201
    staff = client.get("/api/v1/tickets", headers=t["h"]["viewer"]).json()
    assert next(x for x in staff if x["id"] == r.json()["id"])["client_id"] == t["alpha"].id


def test_drafts_never_visible_in_portal(client, t):
    cid = t["alpha"].id
    rep = client.post("/api/v1/client-reports", headers=t["h"]["rep"], json={"client_id": cid, "period_start": "2026-09-01", "period_end": "2026-09-30"}).json()
    inv = client.post("/api/v1/invoices", headers=t["h"]["admin"], json={"client_id": cid, "issue_date": "2026-10-01", "due_date": "2026-10-15",
                                                                       "lines": [{"kind": "one_time", "description": "Draft", "unit_amount_cents": 500}]}).json()
    assert client.get(f"/api/v1/portal/reports/{rep['id']}", headers=t["h"]["alpha"]).status_code == 404
    assert client.get(f"/api/v1/portal/invoices/{inv['id']}", headers=t["h"]["alpha"]).status_code == 404
    assert client.get("/api/v1/portal/invoices", headers=t["h"]["alpha"]).json() == []


@pytest.mark.parametrize("who,path,expected", [
    ("viewer", "/api/v1/invoices", 403), ("rep", "/api/v1/finance/report", 403), ("manager", "/api/v1/finance/report", 200),
    ("viewer", "/api/v1/clients", 200),
])
def test_role_matrix_reads(client, t, who, path, expected):
    assert client.get(path, headers=t["h"][who]).status_code == expected


def test_role_matrix_writes(client, t):
    body = {"client_id": t["alpha"].id, "issue_date": "2026-10-01", "due_date": "2026-10-10",
            "lines": [{"kind": "one_time", "description": "x", "unit_amount_cents": 100}]}
    assert client.post("/api/v1/invoices", headers=t["h"]["manager"], json=body).status_code == 403
    assert client.post("/api/v1/invoices", headers=t["h"]["admin"], json=body).status_code == 201
    assert client.post(f"/api/v1/clients/{t['alpha'].id}/automation/seo_monitoring", headers=t["h"]["rep"], json={"enabled": True}).status_code == 403
    assert client.post("/api/v1/tickets", headers=t["h"]["viewer"], json={"client_id": t["alpha"].id, "kind": "other", "title": "x"}).status_code == 403


def test_staff_user_creation_cannot_make_unbound_client_users(client, db, admin):
    csrf = login(client, admin.email)
    r = client.post("/settings/user", data={"csrf": csrf, "email": "x@y.example", "role": "client", "password": "long-enough-pw", "active": "1"})
    assert r.status_code == 400


def test_cookie_json_post_needs_csrf_header(client, db, admin, t):
    csrf = login(client, admin.email)
    assert client.post("/api/v1/tickets", json={"client_id": t["alpha"].id, "kind": "other", "title": "x"}).status_code == 403
    r = client.post("/api/v1/tickets", json={"client_id": t["alpha"].id, "kind": "other", "title": "x"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201


def test_open_redirect_variants_blocked(client, db, admin):
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/login", data={"email": admin.email, "password": "correct horse battery", "csrf": csrf, "next": "/\\evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_security_headers_and_no_inline_script(client, db, admin):
    login(client, admin.email)
    r = client.get("/agent")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and r.headers["cache-control"] == "no-store"
    assert "onsubmit=" not in r.text and "onclick=" not in r.text


def test_api_rate_limit(client, t, monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.web.app import api_limiter

    api_limiter.hits.clear()
    monkeypatch.setattr(get_settings(), "api_rate_limit_per_minute", 5)
    codes = [client.get("/api/v1/clients", headers=t["h"]["viewer"]).status_code for _ in range(7)]
    api_limiter.hits.clear()
    assert codes[-1] == 429


def test_card_numbers_rejected_and_redacted(client, t):
    assert contains_card_number("4111 1111 1111 1111") and not contains_card_number("1234567890123")
    inv = issued_invoice(client, t["h"]["admin"], t["alpha"].id, 1000)
    r = client.post("/api/v1/payments", headers=t["h"]["admin"], json={"client_id": t["alpha"].id, "invoice_id": inv["id"], "amount_cents": 1000,
                                                                     "received_date": str(date.today()), "reference": "card 4111-1111-1111-1111"})
    assert r.status_code == 400 and "card" in r.json()["detail"].lower()
    s = redact("password=hunter2 Bearer sk-ant-api03-SECRETSECRET 4111111111111111")
    assert "hunter2" not in s and "SECRETSECRET" not in s and "4111111111111111" not in s
    import logging

    rec = logging.LogRecord("x", logging.INFO, "f", 1, "token=%s", ("abc123secret",), None)
    RedactingFilter().filter(rec)
    assert "abc123secret" not in rec.getMessage()


def test_stripe_webhook_signature_and_dedup(client, t, monkeypatch):
    from dealerflow.config import get_settings
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", SecretStr("whsec_test"))
    body = json.dumps({"id": "evt_1", "type": "invoice.paid", "created": 1, "data": {"object": {"id": "in_1", "amount_paid": 0,
                                                                                               "metadata": {"client_id": t["alpha"].id}}}}).encode()
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 400
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(body, "whsec_test", ts=1_000_000)}).status_code == 400
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(body, "whsec_test")}).json()["status"] == "accepted"
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(body, "whsec_test")}).json()["status"] == "duplicate"


def test_stripe_webhook_disabled_without_secret(client, t):
    body = b'{"id":"evt_x","type":"invoice.paid"}'
    assert client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sign(body, "")}).status_code == 400


def test_upload_validation(client, db, t, admin):
    cats = client.get("/api/v1/expense-categories", headers=t["h"]["admin"]).json()
    csrf = login(client, admin.email)
    base = {"csrf": csrf, "expense_date": "2026-10-01", "vendor": "Host Co", "category_id": str(cats[0]["id"]), "amount": "20", "business_purpose": "Hosting"}
    client.post("/finance/expenses", data=base, files={"receipt": ("x.pdf", b"<script>", "application/pdf")})
    assert client.get("/api/v1/expenses?start=2026-10-01&end=2026-10-01", headers=t["h"]["admin"]).json() == []  # rolled back with the bad file
    client.post("/finance/expenses", data=base, files={"receipt": ("..\\..\\r.pdf", b"%PDF-1.4 x", "application/pdf")})
    from dealerflow.models import Attachment

    att = db.query(Attachment).one()
    assert att.filename == "r.pdf" and att.size_bytes == 10


def test_production_refuses_insecure_config(monkeypatch):
    from pydantic import SecretStr

    from dealerflow.config import Settings
    from dealerflow.web import app as webapp

    s = Settings(env="production", secret_key=SecretStr("short"), base_url="http://x", database_url="sqlite:///x.db",
                 outreach_sending_enabled=True)
    assert len(s.production_problems()) == 2 and len(s.production_warnings()) == 2
    ok = Settings(env="production", secret_key=SecretStr("x" * 40), base_url="http://127.0.0.1:8000")  # Docker default still starts
    assert ok.production_problems() == []
    monkeypatch.setattr(webapp, "get_settings", lambda: s)
    with pytest.raises(RuntimeError):
        webapp.create_app()


def test_audit_records_scrub_secrets(db):
    from dealerflow import audit
    from dealerflow.models import AuditLog

    audit.record(db, "x.test", details={"smtp_password": "p", "nested": {"api_key": "k"}, "ok": 1})
    db.commit()
    row = db.query(AuditLog).filter_by(action="x.test").one()
    assert row.data == {"smtp_password": "[redacted]", "nested": {"api_key": "[redacted]"}, "ok": 1}


def test_client_users_cannot_log_in_to_staff_pages_via_next(client, t):
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/login", data={"email": "alpha@p4.example", "password": "correct horse battery", "csrf": csrf, "next": "/finance"},
                    follow_redirects=False)
    assert r.headers["location"] == "/portal"


def test_make_user_helper_still_creates_staff(db):
    u = make_user(db, "v@x.example", "viewer")
    assert not u.is_client
