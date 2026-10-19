"""Shared fixture data for the Phase 3 customer-messaging tests (ported from app/tests). Synthetic data only."""

from __future__ import annotations

import secrets
from datetime import timedelta

import pytest

from dealerflow.db import session_factory, session_scope, utcnow
from dealerflow.models import User
from dealerflow.models_customer import ResponseAutomationSettings
from dealerflow.models_dealer import Client, InventorySource, Vehicle
from dealerflow.models_ops import IntegrationConnection
from dealerflow.security import hash_password

PASSWORD = "correct horse battery"
HOURS = {str(wd): (None if wd == 6 else ["09:00", "19:00"]) for wd in range(7)}  # Mon-Sat 9-7, Sunday closed


def add_vehicle(db, client_id: int, *, stock: str, vin: str | None = None, year=2019, make="Honda", model="Civic", trim="EX",
                price_cents: int | None = 1899500, mileage: int | None = 45210, status: str = "available",
                features: list[str] | None = None, seen_hours_ago: float = 1, conflict: str | None = None) -> Vehicle:
    src = db.query(InventorySource).filter_by(client_id=client_id, source_type="manual").first()
    if src is None:
        src = InventorySource(client_id=client_id, name="Manual entry", source_type="manual", full_snapshot=False)
        db.add(src)
        db.flush()
    seen = utcnow() - timedelta(hours=seen_hours_ago)
    v = Vehicle(client_id=client_id, source_id=src.id, external_id=stock, stock_number=stock, vin=vin, year=year, make=make, model=model,
                trim=trim, price_cents=price_cents, mileage=mileage, inventory_status=status,
                features=list(features if features is not None else ["Backup camera", "Bluetooth", "Sunroof"]),
                data_conflict_note=conflict, content_fingerprint=f"fp-{stock}", photo_fingerprint="none",
                first_seen_at=seen, last_seen_at=seen, last_synced_at=seen)
    db.add(v)
    db.flush()
    return v


def build_world() -> dict:
    """Two dealerships (a, b), each with a dealer admin + reviewer + viewer, approved business info and hours,
    a fresh verified Civic, response settings (website-chat key) and a website-chat connection. Plus agency staff."""
    out: dict = {}
    pw = hash_password(PASSWORD)
    with session_scope() as db:
        for key, name in (("a", "Alpha Auto (TEST)"), ("b", "Bravo Motors (TEST)")):
            c = Client(name=name, slug=f"{key}-dealer", status="active", city="Sugar Land", state="TX", postal_code="77478",
                       phone="(281) 555-0100", address_line=f"{key.upper()}1 Test Rd", business_hours=HOURS,
                       business_info_approved=True, contact_email=f"owner@{key}.example")
            db.add(c)
            db.flush()
            db.add(ResponseAutomationSettings(client_id=c.id, auto_reply_outside_business_hours=True, min_seconds_between_auto_replies=5,
                                              chat_widget_key=secrets.token_urlsafe(24)))
            db.add(IntegrationConnection(client_id=c.id, kind="customer_messaging", provider="website_chat", status="sandbox",
                                         external_account_id=f"web:{c.id}"))
            users = {}
            for role in ("dealer_admin", "dealer_reviewer", "dealer_viewer"):
                u = User(email=f"{role.split('_')[1]}@{key}.example", name=f"{name} {role}", role="viewer", password_hash=pw,
                         client_id=c.id, client_role=role)
                db.add(u)
                users[role] = u
            db.flush()
            civic = add_vehicle(db, c.id, stock=f"{key.upper()}100", vin=f"2HGFC2F70KH50000{1 if key == 'a' else 2}")
            out[key] = {"id": c.id, "admin": users["dealer_admin"].id, "reviewer": users["dealer_reviewer"].id,
                        "viewer": users["dealer_viewer"].id, "civic": civic.id}
        for role in ("admin", "manager", "rep", "viewer"):
            u = User(email=f"{role}@agency.example", name=f"Agency {role}", role=role, password_hash=pw)
            db.add(u)
            db.flush()
            out[f"staff_{role}"] = u.id
    return out


@pytest.fixture
def world():
    return build_world()


def run_worker(max_rounds: int = 10) -> int:
    """Run due jobs until none are left (bounded)."""
    from dealerflow import handlers  # noqa: F401
    from dealerflow.jobs import run_pending

    total = 0
    for _ in range(max_rounds):
        counts = run_pending()
        n = sum(counts.values())
        total += n
        if n == 0:
            break
    return total


def chat(client_id: int, visitor: str, text: str, msg_id: str | None = None, process: bool = False):
    """Ingest a website-chat message directly (bypassing HTTP). Returns (message_id, conversation_id, created)."""
    from dealerflow.services.customer.processing import ingest_inbound, process_message

    s = session_factory()()
    try:
        res = ingest_inbound(s, dealership=s.get(Client, client_id), channel="WEBSITE_CHAT", external_conversation_id=f"web:{visitor}",
                             text=text, provider_message_id=msg_id or f"{client_id}:{visitor}:{abs(hash(text))}")
        if process and res.created:
            process_message(s, res.message.id)
        s.commit()
        return res.message.id, res.conversation.id, res.created
    finally:
        s.close()


def dealer_login(client, email: str, password: str = PASSWORD) -> str:
    """Log in a dealership user; returns the CSRF token of the new session (from a dealer workspace page)."""
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/login", data={"email": email, "password": password, "csrf": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    page = client.get(r.headers["location"])
    return page.text.split('name="csrf" value="')[1].split('"')[0] if 'name="csrf" value="' in page.text else ""


def csrf_from(client, path: str) -> str:
    return client.get(path).text.split('name="csrf" value="')[1].split('"')[0]
