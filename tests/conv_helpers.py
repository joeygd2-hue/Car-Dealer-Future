"""Helpers for the customer-conversation (Phase 3) tests. Synthetic data only; every channel is SIMULATED.

``world`` builds two dealerships the way the unified app stores them: a ``Client`` with approved business info and
weekly hours, response-automation settings, a ``customer_messaging`` website-chat connection, dealership users
(dealer_admin and dealer_reviewer) and one verified Civic entered through Phase 2's manual-entry path.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from dealerflow.db import session_scope, utcnow
from dealerflow.models import (
    Client,
    IntegrationConnection,
    ResponseAutomationSettings,
    User,
)
from dealerflow.security import hash_password
from dealerflow.services.conversations.seeding import add_vehicle, set_weekly_hours

PW = "correct horse battery"
WEEKLY = [["09:00", "19:00"]] * 6 + [None]  # Monday-Saturday 9-7, closed Sunday


@pytest.fixture
def world(db):
    pw = hash_password(PW)
    out = {}
    for key, name in (("a", "Alpha Auto"), ("b", "Bravo Motors")):
        d = Client(name=name, slug=f"conv-{key}", status="active", phone="(281) 555-0100", address_line=f"{key.upper()}1 Test Rd",
                   city="Sugar Land", state="TX", postal_code="77478", business_info_approved=True,
                   contact_email=f"owner@{key}.example", website=f"https://{key}.example")
        set_weekly_hours(d, WEEKLY)
        db.add(d)
        db.flush()
        db.add(ResponseAutomationSettings(client_id=d.id, auto_reply_outside_business_hours=True, min_seconds_between_auto_replies=5))
        db.add(IntegrationConnection(client_id=d.id, kind="customer_messaging", provider="WEBSITE_CHAT", status="sandbox",
                                     external_account_id=f"web:{d.id}"))
        admin = User(email=f"admin@{key}.example", name=f"{name} Admin", role="client", client_id=d.id, client_role="dealer_admin",
                     password_hash=pw)
        staff = User(email=f"staff@{key}.example", name=f"{name} Staff", role="client", client_id=d.id,
                     client_role="dealer_reviewer", password_hash=pw)
        db.add_all([admin, staff])
        db.flush()
        civic = add_vehicle(db, d, stock_number=f"{key.upper()}100", vin=f"2HGFC2F70KH50000{1 if key == 'a' else 2}", year=2019,
                            make="Honda", model="Civic", trim="EX", price_cents=1899500, mileage=45210, status="AVAILABLE",
                            verified_at=utcnow() - timedelta(hours=1), approved_features=["Backup camera", "Bluetooth", "Sunroof"])
        out[key] = {"d": d, "admin": admin, "staff": staff, "civic": civic}
    operator = User(email="ops@platform.example", name="Operator", role="admin", password_hash=pw)
    db.add(operator)
    db.commit()
    out["platform"] = operator
    return out


def run_worker(rounds: int = 10) -> dict:
    """Run the jobs that are due now with the real job runner, until none are left (bounded).

    Unlike ``tests.dealer_helpers.drain`` this does not pull future jobs forward: test-drive reminders and backoff
    retries stay scheduled, as they would in production."""
    from dealerflow import handlers  # noqa: F401
    from dealerflow.jobs import run_pending

    total: dict[str, int] = {}
    for _ in range(rounds):
        counts = run_pending()
        if not counts:
            break
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
    return total


def chat(dealership, visitor: str, text: str, msg_id: str | None = None):
    """Ingest a website-chat message directly (bypassing HTTP). Returns (message_id, conversation_id, created)."""
    from dealerflow.services.conversations.processing import ingest_inbound

    with session_scope() as s:
        d = s.get(Client, dealership.id)
        res = ingest_inbound(s, dealership=d, channel="WEBSITE_CHAT", external_conversation_id=f"web:{visitor}", text=text,
                             provider_message_id=msg_id or f"{visitor}:{abs(hash(text))}")
        s.flush()
        return res.message.id, res.conversation.id, res.created


def login(client, email: str, password: str = PW) -> str:
    """Log in through the form and return the CSRF token of the new session."""
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/login", data={"email": email, "password": password, "csrf": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    r = client.get(r.headers["location"])
    return r.text.split('name="csrf" value="')[1].split('"')[0]


def enable_auto_gate(db, client_id: int) -> None:
    """Phase 4's per-client ``inquiry_auto_response`` switch (normally enabled on the Clients page after onboarding)."""
    from dealerflow.models import AutomationSetting

    db.add(AutomationSetting(client_id=client_id, feature="inquiry_auto_response", enabled=True, enabled_at=utcnow()))


def enable_mode_b(db, world, key: str = "a") -> None:
    """Authorize Mode B for one dealership the way the product does it: a passing evaluation run, a dealer-admin
    authorization, approved auto-send templates and the Phase 4 auto-response gate."""
    from sqlalchemy import select

    from dealerflow.models import ApprovedTemplate, EvaluationRun, ModeAuthorization
    from dealerflow.services.conversations.engine import ENGINE_VERSION

    d = world[key]["d"]
    run = EvaluationRun(client_id=d.id, engine_version=ENGINE_VERSION, total_cases=1, metrics={}, passed=True)
    db.add(run)
    db.flush()
    db.add(ModeAuthorization(client_id=d.id, mode="B_FAQ_AUTOMATION", statement="test", evaluation_run_id=run.id,
                             authorized_by_id=world[key]["admin"].id))
    s = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == d.id))
    s.mode = "B_FAQ_AUTOMATION"
    for intent, body in (("price", "The listed price for the {vehicle} is {price}."),
                         ("availability", "The {vehicle} is currently available."),
                         ("greeting", "Hi{name}! Thanks for contacting {dealer_name}.")):
        db.add(ApprovedTemplate(client_id=d.id, intent=intent, language="en", body=body, auto_send_allowed=True,
                                approved_by_id=world[key]["admin"].id, approved_at=utcnow()))
    enable_auto_gate(db, d.id)
    db.commit()
