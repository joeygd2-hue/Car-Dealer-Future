"""Helpers for Phase 4 tests (synthetic data only)."""

from __future__ import annotations

from datetime import date, timedelta

from dealerflow.models import Client, User
from dealerflow.security import hash_password, new_api_key

PW = "correct horse battery"


def user_with_key(db, email: str, role: str, client_id: int | None = None, client_role: str | None = None) -> tuple[User, dict]:
    key, digest = new_api_key()
    if role == "client" and client_role is None:
        client_role = "dealer_viewer"
    u = User(email=email, name=email.split("@")[0], role=role, client_id=client_id, client_role=client_role,
             password_hash=hash_password(PW), api_key_hash=digest)
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
    u, h = user_with_key(db, "alpha-admin@p4.example", "client", a.id, client_role="dealer_admin")
    out["h"]["alpha_admin"], out["u"]["alpha_admin"] = h, u
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


PHASE2_CSV = (
    "stock,vin,year,make,model,trim,body,mileage,price,exterior_color,photos,condition\n"
    "A100,2HGFC2F59KH123456,2019,Honda,Civic,EX,Sedan,42000,15995,Blue,https://img.example/a100.jpg,Used\n"
)


def phase2_import_and_approve(client, h_dealer: dict, client_id: int, csv_text: str = PHASE2_CSV) -> dict:
    """Upload inventory through the Phase 2 API, let the worker draft EN/ES listings, then approve them as the
    dealership (preview -> confirm with expected count, warnings acknowledged). Returns the import run JSON."""
    from tests.dealer_helpers import drain

    base = f"/api/v1/dealers/{client_id}"
    r = client.post(f"{base}/imports", headers=h_dealer, files={"file": ("inventory.csv", csv_text.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    run = r.json()
    drain()  # the worker generates listing drafts
    ids = [li["id"] for li in client.get(f"{base}/listings?status=awaiting_approval", headers=h_dealer).json()["items"]]
    preview = client.post(f"{base}/listings-batch/preview", headers=h_dealer, json={"listing_ids": ids}).json()
    r = client.post(f"{base}/listings-batch/approve", headers=h_dealer, json={
        "listing_ids": ids, "expected_count": preview["eligible_count"], "confirm": True, "acknowledge_all_warnings": True})
    assert r.status_code == 200, r.text
    run["approved"] = r.json()["approved"]
    return run
