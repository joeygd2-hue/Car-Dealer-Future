"""Pilot mode: during a controlled pilot every LIVE send may only reach an internal test recipient.

``DF_PILOT_MODE=true`` (the default) + the ``pilot_recipients`` allowlist (managed on /admin/pilot) +
``DF_TEST_INBOXES`` (emails). Applies to every send path: customer replies, dealer notifications, client report
email and outreach email. Simulated (mock/sandbox) sends are never affected — nothing leaves the system.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..models import AgentState, PilotRecipient

KINDS = ("EMAIL", "PHONE", "CHAT_VISITOR", "META_PSID")
RECIPIENT_KIND_BY_CHANNEL = {"WEBSITE_CHAT": "CHAT_VISITOR", "META_MESSENGER": "META_PSID", "TWILIO_SMS": "PHONE", "EMAIL": "EMAIL"}


def normalize(kind: str, value: str) -> str:
    value = (value or "").strip()
    if kind == "EMAIL":
        return value.lower()
    if kind == "PHONE":
        return re.sub(r"\D", "", value)[-10:]
    if kind == "CHAT_VISITOR":
        return value.removeprefix("web:")
    return value


def is_allowlisted(db: Session, kind: str, value: str) -> bool:
    norm = normalize(kind, value)
    if not norm:
        return False
    if kind == "EMAIL" and norm in get_settings().test_inbox_list:
        return True
    return db.scalar(select(PilotRecipient.id).where(PilotRecipient.kind == kind, PilotRecipient.value == norm)) is not None


def live_send_blocker(db: Session, channel: str, recipient: str) -> str | None:
    """Reason a LIVE send must not happen under pilot mode, or None."""
    if not get_settings().pilot_mode:
        return None
    kind = RECIPIENT_KIND_BY_CHANNEL.get(channel, "EMAIL")
    if is_allowlisted(db, kind, recipient):
        return None
    return f"pilot mode: {kind.lower()} recipient is not on the internal test allowlist"


def add(db: Session, kind: str, value: str, *, label: str, actor_id: int) -> PilotRecipient:
    if kind not in KINDS:
        raise ValueError("unknown recipient kind")
    norm = normalize(kind, value)
    if not norm:
        raise ValueError("empty recipient")
    row = db.scalar(select(PilotRecipient).where(PilotRecipient.kind == kind, PilotRecipient.value == norm))
    if row is None:
        row = PilotRecipient(kind=kind, value=norm, label=label[:200], added_by_id=actor_id)
        db.add(row)
        db.flush()
        audit.event(db, "pilot.recipient_added", actor_id=actor_id, entity_type="pilot_recipient", entity_id=row.id, kind=kind)
    return row


def remove(db: Session, row: PilotRecipient, *, actor_id: int) -> None:
    audit.event(db, "pilot.recipient_removed", actor_id=actor_id, entity_type="pilot_recipient", entity_id=row.id, kind=row.kind)
    db.delete(row)


def emergency_stopped(db: Session) -> bool:
    st = db.get(AgentState, 1)
    return st is not None and st.status == "emergency_stopped"
