"""Helpers for Phase 4 tests (synthetic data only)."""

from __future__ import annotations

from datetime import date, timedelta

from dealerflow.models import Client, User
from dealerflow.security import hash_password, new_api_key

PW = "correct horse battery"


def user_with_key(db, email: str, role: str, client_id: int | None = None, client_role: str = "dealer_admin") -> tuple[User, dict]:
    """``client_id`` set = a dealership user (Phase 2 model: role viewer + client_role)."""
    key, digest = new_api_key()
    u = User(email=email, name=email.split("@")[0], role="viewer" if client_id else role, client_id=client_id,
             client_role=client_role if client_id else None, password_hash=hash_password(PW), api_key_hash=digest)
    db.add(u)
    db.commit()
    return u, {"Authorization": f"Bearer {key}"}


def two_clients(db) -> dict:
    """Alpha and Bravo dealerships, staff of every role, and a dealership admin user per dealership."""
    from dealerflow.services import onboarding

    a = Client(name="Alpha Motors (TEST)", slug="alpha-motors", status="onboarding", city="Sugar Land", phone="281-555-0101",
               contact_email="owner@alpha.example")
    b = Client(name="Bravo Auto (TEST)", slug="bravo-auto", status="onboarding", city="Stafford", phone="281-555-0202",
               contact_email="owner@bravo.example")
    db.add_all([a, b])
    db.flush()
    onboarding.ensure_checklist(db, a)
    onboarding.ensure_checklist(db, b)
    db.commit()
    out = {"alpha": a, "bravo": b, "h": {}, "u": {}}
    for name, role, cid in (("admin", "admin", None), ("manager", "manager", None), ("rep", "rep", None), ("viewer", "viewer", None),
                            ("alpha", "client", a.id), ("bravo", "client", b.id)):
        u, h = user_with_key(db, f"{name}@p4.example", role, cid)
        out["h"][name], out["u"][name] = h, u
    return out


def portal_login(client, email: str) -> str:
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/login", data={"email": email, "password": PW, "csrf": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/dealers/"), r.text[:300]
    page = client.get("/portal").text
    return page.split('name="csrf" value="')[1].split('"')[0]


def issued_invoice(client, h, client_id: int, cents: int = 10000, issue: date | None = None, due_days: int = 15) -> dict:
    issue = issue or date.today()
    r = client.post("/api/v1/invoices", headers=h, json={
        "client_id": client_id, "issue_date": str(issue), "due_date": str(issue + timedelta(days=due_days)), "issue": True,
        "lines": [{"kind": "one_time", "description": "Synthetic service", "unit_amount_cents": cents}]})
    assert r.status_code == 201, r.text
    return r.json()
