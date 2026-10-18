"""Campaign enrollments (multi-step sequences) and their stop conditions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Campaign, Enrollment, Prospect
from .crm import add_activity


def stop_enrollments(db: Session, p: Prospect, reason: str) -> int:
    n = 0
    for e in db.scalars(select(Enrollment).where(Enrollment.prospect_id == p.id, Enrollment.status == "active")):
        e.status, e.stop_reason, e.stopped_at = "stopped", reason[:120], utcnow()
        n += 1
    # Unsent drafts/queued sequence messages are cancelled too.
    from ..models import OutreachMessage
    for m in db.scalars(select(OutreachMessage).where(
            OutreachMessage.prospect_id == p.id, OutreachMessage.campaign_id.is_not(None),
            OutreachMessage.status.in_(("draft", "approved", "queued")))):
        m.status, m.error = "cancelled", f"sequence stopped: {reason}"
        n += 1
    if n:
        add_activity(db, p, "system", f"Sequence stopped ({reason})")
    return n


def enroll(db: Session, campaign: Campaign, p: Prospect, *, now=None) -> Enrollment | None:
    if p.do_not_contact:
        return None
    existing = db.scalar(select(Enrollment).where(Enrollment.campaign_id == campaign.id, Enrollment.prospect_id == p.id))
    if existing:
        return None
    e = Enrollment(campaign_id=campaign.id, prospect_id=p.id, status="active", step=0, next_action_at=now or utcnow())
    db.add(e)
    db.flush()
    add_activity(db, p, "system", f"Enrolled in campaign '{campaign.name}'")
    return e
