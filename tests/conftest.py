"""Test fixtures. Every test gets a fresh SQLite file database (file-based so the
worker's separate connections see the same data). No network: HTTP calls go to
httpx MockTransports; outreach uses sandbox providers and simulated prospects."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("APP_ENCRYPTION_KEY", base64.b64encode(b"k" * 32).decode())
os.environ["DATABASE_URL"] = "sqlite:///./data/test-placeholder.db"
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import db as dbmod  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    dbmod.configure(f"sqlite:///{tmp_path}/test.db")
    dbmod.init_db()
    monkeypatch.setattr(settings, "encryption_key", os.environ["APP_ENCRYPTION_KEY"])
    monkeypatch.setattr(settings, "listing_ai_enabled", False)
    monkeypatch.setattr(settings, "outreach_ai_enabled", False)
    monkeypatch.setattr(settings, "meta_live_publishing_allowed", False)
    monkeypatch.setattr(settings, "outreach_live_allowed", False)
    from app.publishing.adapters.sandbox import SandboxAdapter
    SandboxAdapter._state.clear()
    yield
    dbmod.engine.dispose()


@pytest.fixture
def session():
    s = dbmod.SessionLocal()
    yield s
    s.close()


@pytest.fixture
def world(session):
    """Two dealerships, users with various roles, and one platform admin."""
    from app.core.security import create_user
    from app.models import Client, ClientMembership
    a = Client(name="Alpha Auto", slug="alpha", city="Sugar Land", state="TX")
    b = Client(name="Bravo Motors", slug="bravo", city="Stafford", state="TX")
    session.add_all([a, b])
    session.flush()
    pw = "correct-horse-battery"
    users = {
        "admin": create_user(session, email="admin@agency.test", name="Admin", password=pw, platform_role="platform_admin"),
        "staff": create_user(session, email="staff@agency.test", name="Staff", password=pw, platform_role="platform_staff"),
        "a_admin": create_user(session, email="owner@alpha.test", name="Alpha Owner", password=pw),
        "a_viewer": create_user(session, email="viewer@alpha.test", name="Alpha Viewer", password=pw),
        "a_agency": create_user(session, email="op@agency.test", name="Operator", password=pw),
        "b_admin": create_user(session, email="owner@bravo.test", name="Bravo Owner", password=pw),
    }
    session.add_all([
        ClientMembership(client_id=a.id, user_id=users["a_admin"].id, role="dealer_admin"),
        ClientMembership(client_id=a.id, user_id=users["a_viewer"].id, role="dealer_viewer"),
        ClientMembership(client_id=a.id, user_id=users["a_agency"].id, role="agency_operator"),
        ClientMembership(client_id=b.id, user_id=users["b_admin"].id, role="dealer_admin"),
    ])
    session.commit()
    return {"a": a, "b": b, "users": users, "password": pw}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


def login(client, email, password="correct-horse-battery") -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def ctx_for(client_id: str, name: str = "test"):
    from app.core.tenancy import job_context
    return job_context(client_id, name)


def run_worker(max_rounds: int = 10, now: datetime | None = None) -> int:
    """Drain due jobs with the real worker (bounded)."""
    from app.worker import build_handlers
    from app.core.worker import Worker
    w = Worker(build_handlers(), worker_id="test-worker")
    total = 0
    for _ in range(max_rounds):
        n = w.tick(now=now)
        total += n
        if not n:
            break
    return total


SAMPLE_CSV = """Stock #,VIN,Year,Make,Model,Trim,Body,Miles,Price,Ext Color,Int Color,Trans,Drive,Fuel,Options,Photo URLs,Address,City,State,Zip,Condition
A100,1HGCM82633A004352,2003,Honda,Accord,EX,Sedan,145000,"$5,995",Silver,Gray,Automatic,FWD,Gas,Bluetooth|Sunroof|Heated Seats,https://img.example/a100-1.jpg|https://img.example/a100-2.jpg,123 Main St,Sugar Land,TX,77478,Used
B200,,2019,toyota,camry,SE,Sedan,45000,18995,Black,Black,auto,fwd,gasoline,Backup Camera|Apple CarPlay,https://img.example/b200.jpg,123 Main St,Sugar Land,TX,77478,Used
C300,,2018,Ford,F-150,XLT,Truck,82000,26500,White,Gray,Automatic,4x4,Gasoline,Tow Package|Bed Liner,https://img.example/c300.jpg,123 Main St,Sugar Land,TX,77478,Used
"""


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)
