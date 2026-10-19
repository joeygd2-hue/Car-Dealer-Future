"""Helpers for Phase 4 tests (synthetic data only)."""

from __future__ import annotations

from datetime import date, timedelta

from dealerflow.models import Client, User
from dealerflow.security import hash_password, new_api_key

PW = "correct horse battery"


def user_with_key(db, email: str, role: str, client_id: int | None = None) -> tuple[User, dict]:
    key, digest = new_api_key()
    u = User(email=email, name=email.split("@")[0], role=role, client_id=client_id, password_hash=hash_password(PW), api_key_hash=digest)
    db.add(u)
    db.commit()
    return u, {"Authorization": f"Bearer {key}"}


def two_clients(db) -> dict:
    """Alpha and Bravo dealerships, staff of every role, and a portal user per dealership."""
    from dealerflow.services import onboarding

    a = Client(name="Alpha Motors (TEST)", city="Sugar Land", phone="281-555-0101", contact_email="owner@alpha.example")
    b = Client(name="Bravo Auto (TEST)", city="Stafford", phone="281-555-0202", contact_email="owner@bravo.example")
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
    assert r.status_code == 303 and r.headers["location"] == "/portal", r.text[:300]
    page = client.get("/portal").text
    return page.split('name="csrf" value="')[1].split('"')[0]


def issued_invoice(client, h, client_id: int, cents: int = 10000, issue: date | None = None, due_days: int = 15) -> dict:
    issue = issue or date.today()
    r = client.post("/api/v1/invoices", headers=h, json={
        "client_id": client_id, "issue_date": str(issue), "due_date": str(issue + timedelta(days=due_days)), "issue": True,
        "lines": [{"kind": "one_time", "description": "Synthetic service", "unit_amount_cents": cents}]})
    assert r.status_code == 201, r.text
    return r.json()


def phase2_listings(client, headers: dict, client_id: int, csv_text: str, *, approve: int | None = None) -> dict:
    """Run the real Phase 2 flow over HTTP: dealership settings, CSV upload, worker drafting, approvals.

    Approves the first ``approve`` drafted listings (all when None), acknowledging their content warnings.
    Returns the import result, the drafted listings and the channel publications."""
    from dealerflow.tenancy import system_context
    from tests.dealer_helpers import configure_dealer, drain

    configure_dealer(system_context(client_id))
    imp = client.post(f"/api/v1/dealers/{client_id}/imports", headers=headers,
                      files={"file": ("inventory.csv", csv_text.encode(), "text/csv")}).json()
    drain()
    items = client.get(f"/api/v1/dealers/{client_id}/listings", headers=headers).json()["items"]
    drafted = [li for li in items if li["current_revision"]]
    for li in drafted[:approve]:
        rev = li["current_revision"]
        r = client.post(f"/api/v1/dealers/{client_id}/listings/{li['id']}/approve", headers=headers,
                        json={"revision_id": rev["id"], "acknowledged_warnings": rev["warning_keys"]})
        assert r.status_code == 200, (r.text, rev["guardrails"])
    pubs = client.get(f"/api/v1/dealers/{client_id}/publications", headers=headers).json()
    drafted = client.get(f"/api/v1/dealers/{client_id}/listings", headers=headers).json()["items"]  # statuses after approval
    return {"import": imp, "listings": [li for li in drafted if li["current_revision"]], "publications": pubs}
