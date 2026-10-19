import pytest

from dealerflow.backup import backup, restore
from dealerflow.config import get_settings
from dealerflow.db import reset_engine, session_scope
from dealerflow.models import Prospect
from dealerflow.services.discovery import run_source


def test_sqlite_backup_and_restore(tmp_path):
    # Checked at run time: conftest removes DF_TEST_PG_URL from the environment, so a re-imported
    # tests.conftest.PG_URL is always None and a skipif on it never skipped on Postgres.
    if not get_settings().database_url.startswith("sqlite"):
        pytest.skip("SQLite backup path")
    with session_scope() as db:
        run_source(db, "fixture")
    res = backup(str(tmp_path / "bk"))
    assert res["counts"]["prospects"] == 7 and oct(__import__("os").stat(res["file"]).st_mode)[-3:] == "600"
    with session_scope() as db:  # damage: delete everything after the backup
        db.query(Prospect).delete()
    with pytest.raises(ValueError):
        restore(res["file"])
    reset_engine()
    out = restore(res["file"], confirm=True)
    assert out["counts"]["prospects"] == 7
    reset_engine()
    with session_scope() as db:
        assert db.query(Prospect).count() == 7
    assert get_settings().database_url.startswith("sqlite")


def test_retention_redacts_old_bodies_but_keeps_suppressions(db, admin):
    from datetime import timedelta

    from dealerflow.db import utcnow
    from dealerflow.models import InboundMessage, Suppression
    from dealerflow.retention import REDACTED, apply

    db.add(InboundMessage(provider="mock_email", provider_message_id="old", from_email="a@b.example", body="old secret",
                          received_at=utcnow() - timedelta(days=800)))
    db.add(InboundMessage(provider="mock_email", provider_message_id="new", from_email="a@b.example", body="recent",
                          received_at=utcnow()))
    db.add(Suppression(value="a@b.example", reason="unsubscribe", created_at=utcnow() - timedelta(days=3000)))
    db.commit()
    res = apply(db)
    assert res["inbound_redacted"] == 1
    bodies = {m.provider_message_id: m.body for m in db.query(InboundMessage)}
    assert bodies == {"old": REDACTED, "new": "recent"}
    assert db.query(Suppression).count() == 1
