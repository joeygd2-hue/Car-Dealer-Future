"""Stage 4: audit rows are sealed (HMAC), append-only, and tampering outside the app is detected."""

from __future__ import annotations

import pytest
from sqlalchemy import text


def test_audit_rows_are_sealed_and_verify_after_reload(db):
    from dealerflow.audit import audit, event, verify_integrity

    audit(db, "test.one", data={"n": 1, "f": 1.5, "nested": {"b": [1, "x"]}})
    event(db, "test.two", client_id=7, reason="ünïcode ✓")
    db.commit()
    db.expire_all()
    rep = verify_integrity(db)
    assert rep["ok"] and rep["checked"] >= 2 and not rep["tampered_ids"]


def test_audit_rows_cannot_be_updated_or_deleted_through_the_app(db):
    from dealerflow.audit import AuditLogImmutableError, audit
    from dealerflow.models import AuditLog

    row = audit(db, "test.immutable")
    db.commit()
    row.action = "test.changed"
    with pytest.raises(AuditLogImmutableError):
        db.commit()
    db.rollback()
    with pytest.raises(AuditLogImmutableError):
        db.delete(db.get(AuditLog, row.id))
        db.commit()
    db.rollback()


def test_tampering_outside_the_app_is_detected(db):
    from dealerflow.audit import audit, verify_integrity

    row = audit(db, "outreach.sent", data={"to": "a@example.com"})
    db.commit()
    db.execute(text("UPDATE audit_log SET data = :d WHERE id = :i"), {"d": '{"to": "b@example.com"}', "i": row.id})
    db.commit()
    db.expire_all()
    rep = verify_integrity(db)
    assert not rep["ok"] and rep["tampered_ids"] == [row.id]
