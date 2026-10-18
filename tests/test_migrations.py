"""Alembic migrations: fresh upgrade matches the models, upgrade over existing data works, downgrade works."""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def test_fresh_upgrade_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path}/fresh.db"
    assert _alembic(url, "upgrade", "head").returncode == 0
    check = _alembic(url, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"send_attempts", "pilot_recipients", "rate_counters", "password_reset_tokens", "audit_logs"} <= tables


def test_upgrade_preserves_existing_rows_and_seals_audit(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    assert _alembic(url, "upgrade", "4122abfb2c23").returncode == 0  # the Phase 3 baseline
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (email, name, password_hash, is_platform_admin, is_active, created_at, updated_at) "
                       "VALUES ('u@x.example', 'U', 'x', 0, 1, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
        c.execute(text("INSERT INTO audit_logs (created_at, actor_type, action, outcome, external, details) "
                       "VALUES ('2026-10-01 00:00:00', 'SYSTEM', 'legacy.event', 'OK', 0, '{}')"))
    up = _alembic(url, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    with eng.connect() as c:
        assert c.execute(text("SELECT mfa_enabled, mfa_recovery_hashes FROM users")).one() == (0, "[]")
        assert c.execute(text("SELECT entry_hmac FROM audit_logs")).scalar() is not None
    from sqlalchemy.orm import Session

    from app.services.audit import verify_integrity

    with Session(eng) as s:
        assert verify_integrity(s)["ok"]  # legacy row sealed by the migration verifies with the app's HMAC
    down = _alembic(url, "downgrade", "4122abfb2c23")
    assert down.returncode == 0, down.stderr
    assert "send_attempts" not in inspect(create_engine(url)).get_table_names()


def test_upgrade_creates_missing_sqlite_directory(tmp_path):
    """Regression (clean-checkout reproduction): the default DATABASE_URL points into ./data/, which doesn't exist yet."""
    url = f"sqlite:///{tmp_path}/not-yet-created/sub/app.db"
    r = _alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "not-yet-created" / "sub" / "app.db").exists()


def test_seed_refuses_unmigrated_database(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/empty.db"}
    r = subprocess.run([sys.executable, "-m", "scripts.seed_demo"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "alembic upgrade head" in (r.stderr + r.stdout)
    assert "users" not in inspect(create_engine(f"sqlite:///{tmp_path}/empty.db")).get_table_names()
