"""Migration 0006 (safety layer) over existing data, and upgrade into a missing SQLite directory (ported from
app/tests/test_migrations.py; the fresh-upgrade-matches-models check is tests/test_migrations.py). Synthetic data only."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from tests.conftest import PG_URL

pytestmark = pytest.mark.skipif(bool(PG_URL), reason="builds its own SQLite databases")


def test_upgrade_preserves_existing_rows_and_seals_legacy_audit(tmp_path):
    from dealerflow.audit import verify_integrity
    from dealerflow.migrate import alembic_config

    url = f"sqlite:///{tmp_path / 'old.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0005")  # the schema before the safety layer
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (email, name, role, password_hash, active, created_at, updated_at) "
                       "VALUES ('u@x.example', 'U', 'rep', 'x', 1, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
        c.execute(text("INSERT INTO audit_log (created_at, actor_id, action, entity_type, entity_id, data, ip) "
                       "VALUES ('2026-10-01 00:00:00', 1, 'legacy.event', 'user', 1, '{\"n\": 1}', NULL), "
                       "('2026-10-01 00:00:01', NULL, 'legacy.other', NULL, NULL, NULL, '127.0.0.1')"))
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT email, role, mfa_enabled, mfa_secret, password_changed_at FROM users")).one() == (
            "u@x.example", "rep", 0, None, None)
        assert c.execute(text("SELECT count(*) FROM audit_log WHERE entry_hmac IS NULL")).scalar() == 0
    with Session(eng) as s:
        rep = verify_integrity(s)  # legacy rows sealed by the migration verify with the app's HMAC
        assert rep["ok"] and rep["checked"] == 2 and not rep["tampered_ids"] and not rep["unsealed_ids"]
    tables = set(inspect(eng).get_table_names())
    assert {"user_sessions", "password_reset_tokens", "pilot_recipients"} <= tables

    command.downgrade(cfg, "0005")
    eng.dispose()
    eng = create_engine(url)
    insp = inspect(eng)
    assert not {"user_sessions", "password_reset_tokens", "pilot_recipients"} & set(insp.get_table_names())
    assert "entry_hmac" not in {col["name"] for col in insp.get_columns("audit_log")}
    with eng.connect() as c:  # existing rows survive the round trip
        assert c.execute(text("SELECT email FROM users")).scalar() == "u@x.example"
        assert c.execute(text("SELECT count(*) FROM audit_log")).scalar() == 2
    eng.dispose()


def test_upgrade_creates_missing_sqlite_directory(tmp_path):
    """Regression (clean-checkout reproduction): the default DF_DATABASE_URL points into ./state/, which may not exist."""
    from dealerflow.migrate import upgrade

    target = tmp_path / "not-yet-created" / "sub" / "app.db"
    upgrade(f"sqlite:///{target}")
    assert target.exists()
    eng = create_engine(f"sqlite:///{target}")
    assert {"users", "audit_log", "user_sessions", "send_attempts"} <= set(inspect(eng).get_table_names())
    eng.dispose()


def test_seed_refuses_an_unmigrated_database(tmp_path):
    import pytest
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session

    from dealerflow.seed import NotMigratedError, seed

    eng = create_engine(f"sqlite:///{tmp_path / 'blank.db'}")
    with Session(eng) as s, pytest.raises(NotMigratedError, match="dealerflow migrate"):
        seed(s)
    assert inspect(eng).get_table_names() == []  # and it created nothing
    eng.dispose()
