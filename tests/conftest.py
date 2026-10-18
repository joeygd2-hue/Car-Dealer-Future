"""Test fixtures. All data is synthetic; all providers are sandboxed; no network access is used."""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="dgs-test-")
os.environ.update({
    "ENVIRONMENT": "test",
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_tmp}/test.db"),
    "SECRET_KEY": "test-secret-key-that-is-long-enough-123456",
    "ATTACHMENTS_DIR": f"{_tmp}/attachments",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_secret",
    "INBOUND_WEBHOOK_SECRET": "inbound_test_secret",
    "EMAIL_PROVIDER": "sandbox",
    "INBOX_PROVIDER": "sandbox",
    "CALENDAR_PROVIDER": "sandbox",
    "GOOGLE_PLACES_API_KEY": "",
    "PAGESPEED_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "AI_ENABLED": "false",
    "BASE_URL": "http://testserver",
    "COMPANY_NAME": "Dealer Growth Services",
    "API_RATE_LIMIT_PER_MINUTE": "100000",
    "LOGIN_RATE_LIMIT_PER_MINUTE": "1000",
})

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, get_engine, reset_engine  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    get_settings.cache_clear()
    reset_engine()
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    command.upgrade(cfg, "head")  # the real migration, not create_all
    yield


@pytest.fixture(autouse=True)
def _clean_db():
    yield
    engine = get_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        if engine.dialect.name == "sqlite":
            conn.execute(text("PRAGMA foreign_keys=ON"))
    from app.integrations import calendar, inbox
    from app.security.rate_limit import limiter

    inbox.sandbox_inbox.clear()
    calendar._sandbox.busy.clear()
    limiter.reset()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def app():
    from app.main import create_app

    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def seeded(db):
    """Two dealerships, an owner, an admin, a staff member and one client user per dealership."""
    from app.models.core import Client, User
    from app.security.auth import create_api_token
    from app.security.crypto import hash_password
    from app.services import onboarding
    from app.services.maintenance import bootstrap

    bootstrap(db)
    pw = hash_password("correct-horse-battery")
    a = Client(name="Alpha Motors", city="Sugar Land", phone="281-555-0101", contact_email="owner@alpha.example", website="https://alpha.example")
    b = Client(name="Bravo Auto", city="Stafford", phone="281-555-0202", contact_email="owner@bravo.example")
    db.add_all([a, b])
    db.flush()
    owner = User(email="owner@agency.example", password_hash=pw, role="owner")
    admin = User(email="admin@agency.example", password_hash=pw, role="admin")
    staff = User(email="staff@agency.example", password_hash=pw, role="staff")
    ua = User(email="manager@alpha.example", password_hash=pw, role="client", client_id=a.id)
    ub = User(email="manager@bravo.example", password_hash=pw, role="client", client_id=b.id)
    db.add_all([owner, admin, staff, ua, ub])
    db.flush()
    onboarding.ensure_checklist(db, a)
    onboarding.ensure_checklist(db, b)
    db.commit()
    tokens = {name: create_api_token(db, u, f"test-{name}") for name, u in
              {"owner": owner, "admin": admin, "staff": staff, "alpha": ua, "bravo": ub}.items()}
    return {"alpha": a, "bravo": b, "owner": owner, "admin": admin, "staff": staff, "alpha_user": ua, "bravo_user": ub, "tokens": tokens}


def auth(tokens: dict, who: str) -> dict:
    return {"Authorization": f"Bearer {tokens[who]}"}


@pytest.fixture()
def h(seeded):
    return lambda who: auth(seeded["tokens"], who)
