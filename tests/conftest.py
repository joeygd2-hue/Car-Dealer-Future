"""Test fixtures. Every test gets a fresh SQLite database; all providers resolve to sandbox adapters."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["APP_ENV"] = "test"
os.environ["LIVE_INTEGRATIONS_ENABLED"] = "false"
os.environ["BASE_URL"] = "http://testserver"

from app import db as dbmod  # noqa: E402
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(autouse=True)
def database(tmp_path):
    dbmod.configure(f"sqlite:///{tmp_path}/test.db")
    dbmod.create_all()
    import app.worker as w

    w.register_all_handlers()
    yield
    dbmod.get_engine().dispose()


@pytest.fixture
def db():
    s = dbmod.SessionLocal()
    yield s
    s.close()


@pytest.fixture
def world(db):
    """Two dealerships with users, hours, inventory, website chat connections."""
    from app.models import (
        AutomationSettings, BusinessHours, Dealership, IntegrationConnection, Membership, User, Vehicle,
    )
    from app.security.passwords import hash_password

    pw = hash_password("correct-horse-battery")
    out = {}
    for key, name in (("a", "Alpha Auto"), ("b", "Bravo Motors")):
        d = Dealership(name=name, slug=key, status="ACTIVE", phone="(281) 555-0100", address_line1=f"{key.upper()}1 Test Rd",
                       city="Sugar Land", state="TX", postal_code="77478", business_info_approved=True)
        db.add(d)
        db.flush()
        for wd in range(7):
            db.add(BusinessHours(dealership_id=d.id, weekday=wd, closed=wd == 6,
                                 open_time=None if wd == 6 else "09:00", close_time=None if wd == 6 else "19:00"))
        db.add(AutomationSettings(dealership_id=d.id, auto_reply_outside_business_hours=True,
                                  min_seconds_between_auto_replies=5))
        db.add(IntegrationConnection(dealership_id=d.id, provider="WEBSITE_CHAT", external_account_id=f"web:{d.id}",
                                     status="CONNECTED"))
        admin = User(email=f"admin@{key}.example", name=f"{name} Admin", password_hash=pw)
        staff = User(email=f"staff@{key}.example", name=f"{name} Staff", password_hash=pw)
        db.add_all([admin, staff])
        db.flush()
        db.add(Membership(user_id=admin.id, dealership_id=d.id, role="DEALER_ADMIN", receives_escalations=True))
        db.add(Membership(user_id=staff.id, dealership_id=d.id, role="DEALER_STAFF"))
        civic = Vehicle(dealership_id=d.id, stock_number=f"{key.upper()}100", vin=f"2HGFC2F70KH50000{1 if key == 'a' else 2}",
                        year=2019, make="Honda", model="Civic", trim="EX", price_cents=1899500, mileage=45210,
                        status="AVAILABLE", verified_at=dbmod.utcnow() - timedelta(hours=1),
                        approved_features=["Backup camera", "Bluetooth", "Sunroof"])
        db.add(civic)
        db.flush()
        out[key] = {"d": d, "admin": admin, "staff": staff, "civic": civic}
    platform = User(email="ops@platform.example", name="Operator", password_hash=pw, is_platform_admin=True)
    db.add(platform)
    db.commit()
    out["platform"] = platform
    return out


def run_worker(max_rounds: int = 10):
    """Run the worker until the queue has no due work (bounded)."""
    from app.worker import Worker

    w = Worker(worker_id="test-worker", poll_seconds=0)
    total = 0
    for _ in range(max_rounds):
        n = w.run_once()
        total += n
        if n == 0:
            break
    return total


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def login(client, email: str, password: str = "correct-horse-battery") -> str:
    """Log in and return the CSRF token for the new session."""
    from sqlalchemy import select

    from app.models import User, UserSession

    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with dbmod.SessionLocal() as s:
        user = s.scalar(select(User).where(User.email == email))
        sess = s.scalar(select(UserSession).where(UserSession.user_id == user.id).order_by(UserSession.id.desc()))
        return sess.csrf_token


def chat(dealership, visitor: str, text: str, msg_id: str | None = None):
    """Ingest a website-chat message directly (bypassing HTTP)."""
    from app.services.inbox.processing import ingest_inbound

    with dbmod.SessionLocal() as s:
        from app.models import Dealership

        d = s.get(Dealership, dealership.id)
        res = ingest_inbound(s, dealership=d, channel="WEBSITE_CHAT", external_conversation_id=f"web:{visitor}", text=text,
                             provider_message_id=msg_id or f"{visitor}:{abs(hash(text))}")
        s.commit()
        return res.message.id, res.conversation.id, res.created
