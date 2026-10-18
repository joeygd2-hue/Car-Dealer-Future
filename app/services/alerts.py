from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.costs import Alert


def raise_alert(db: Session, kind: str, message: str, severity: str = "warning", dedupe_key: str | None = None) -> Alert:
    if dedupe_key:
        existing = db.scalar(select(Alert).where(Alert.dedupe_key == dedupe_key))
        if existing:
            return existing
    alert = Alert(kind=kind, message=message, severity=severity, dedupe_key=dedupe_key)
    db.add(alert)
    db.flush()
    return alert
