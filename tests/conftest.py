"""Test setup: each test gets a fresh SQLite database built by the real Alembic migrations.

All providers are the SIMULATED mocks. No network access is needed or attempted.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="dealerflow-tests-")
_TEMPLATE_DB = os.path.join(_TMP, "template.db")

PG_URL = os.environ.get("DF_TEST_PG_URL")  # optional: run the suite against Postgres instead of SQLite

BASE_ENV = {
    "DF_ENV": "test",
    "DF_SECRET_KEY": "test-secret-key",
    "DF_BUSINESS_NAME": "Test Marketing Co (TEST)",
    "DF_BUSINESS_POSTAL_ADDRESS": "123 Test St, Sugar Land, TX 77478",
    "DF_SENDER_NAME": "Test Sender",
    "DF_SENDER_EMAIL": "sender@testco.example",
    "DF_EMAIL_PROVIDER": "mock",
    "DF_CALENDAR_PROVIDER": "mock",
    "DF_ZOOM_PROVIDER": "mock",
    "DF_CALL_PROVIDER": "mock",
    "DF_OUTREACH_SENDING_ENABLED": "false",
    "DF_WEBSITE_RESEARCH_ENABLED": "false",
    "DF_LOG_LEVEL": "WARNING",
}
for k, v in BASE_ENV.items():
    os.environ[k] = v
for k in list(os.environ):
    if k.startswith("DF_") and k not in BASE_ENV and k != "DF_TEST_PG_URL":  # keep it: tests re-import PG_URL
        del os.environ[k]


def _build_template() -> None:
    from dealerflow.config import get_settings
    from dealerflow.db import reset_engine, session_scope
    from dealerflow.migrate import upgrade
    from dealerflow.seed import seed

    os.environ["DF_DATABASE_URL"] = f"sqlite:///{_TEMPLATE_DB}"
    get_settings.cache_clear()
    reset_engine()
    upgrade()
    with session_scope() as db:
        seed(db)
    reset_engine()


if not PG_URL:
    _build_template()


def _reset_pg() -> None:
    from sqlalchemy import create_engine, text

    from dealerflow.db import reset_engine, session_scope
    from dealerflow.migrate import upgrade
    from dealerflow.seed import seed

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    eng.dispose()
    reset_engine()
    upgrade(PG_URL)
    with session_scope() as db:
        seed(db)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.db import reset_engine
    from dealerflow.integrations.calendar import MockCalendarProvider
    from dealerflow.integrations.email import MockEmailProvider
    from dealerflow.integrations.zoom import MockZoomProvider
    from dealerflow.web.deps import login_limiter

    if PG_URL:
        monkeypatch.setenv("DF_DATABASE_URL", PG_URL)
        get_settings.cache_clear()
        _reset_pg()
    else:
        path = tmp_path / "test.db"
        shutil.copy(_TEMPLATE_DB, path)
        monkeypatch.setenv("DF_DATABASE_URL", f"sqlite:///{path}")
        get_settings.cache_clear()
    reset_engine()
    MockEmailProvider.reset()
    MockCalendarProvider.reset()
    MockZoomProvider.reset()
    login_limiter.fails.clear()
    import dealerflow.integrations.http as http
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    yield
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def db():
    from dealerflow import handlers  # noqa: F401
    from dealerflow.db import session_factory

    s = session_factory()()
    yield s
    s.rollback()
    s.close()


def make_user(db, email="admin@test.example", role="admin", password="correct horse battery"):
    from dealerflow.models import User
    from dealerflow.security import hash_password

    u = User(email=email, name=email.split("@")[0], role=role, password_hash=hash_password(password))
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def admin(db):
    return make_user(db)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from dealerflow.web.app import create_app

    return TestClient(create_app())


def login(client, email, password="correct horse battery"):
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/login", data={"email": email, "password": password, "csrf": csrf, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return csrf_of(client)  # the session (and its CSRF token) is replaced at login


def csrf_of(client) -> str:
    r = client.get("/settings")
    return r.text.split('name="csrf" value="')[1].split('"')[0]
