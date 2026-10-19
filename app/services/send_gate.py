"""Single checkpoint for every message that could leave the platform.

Every send path (customer replies, appointment messages, outreach email, staff notification email)
calls ``open_attempt`` immediately before the provider call. It enforces:
  1. the platform-wide EMERGENCY STOP (blocks every new external send, live or simulated);
  2. PILOT MODE: a *live* send is allowed only to a recipient on the internal test allowlist;
and writes a ``SendAttempt`` row (the outbound ledger) that the caller commits BEFORE calling the
provider. ``close_attempt`` records the outcome. An attempt left IN_FLIGHT means "provider may have
accepted it" and is never retried automatically (see ``check_sending_row`` / ``take_over_crashed``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.base import SendResult
from app.models import PilotRecipient, SendAttempt
from app.services import audit, controls

EMERGENCY_STOP_ALL = "platform.emergency_stop_all"

RECIPIENT_KIND_BY_CHANNEL = {"WEBSITE_CHAT": "CHAT_VISITOR", "META_MESSENGER": "META_PSID", "TWILIO_SMS": "PHONE",
                             "EMAIL": "EMAIL"}


def normalize_recipient(kind: str, value: str) -> str:
    value = (value or "").strip()
    if kind == "EMAIL":
        return value.lower()
    if kind == "PHONE":
        digits = re.sub(r"\D", "", value)
        return digits[-10:]
    if kind == "CHAT_VISITOR":
        return value.removeprefix("web:")
    return value


def is_allowlisted(db: Session, kind: str, value: str) -> bool:
    norm = normalize_recipient(kind, value)
    return db.scalar(select(PilotRecipient.id).where(PilotRecipient.kind == kind, PilotRecipient.value == norm)) is not None


def emergency_stopped(db: Session) -> bool:
    return controls.get_flag(db, EMERGENCY_STOP_ALL)


@dataclass
class Gate:
    attempt: SendAttempt
    allowed: bool
    reasons: list[str]


def open_attempt(db: Session, *, kind: str, ref_id: int, channel: str, recipient: str, live: bool,
                 dealership_id: int | None = None) -> Gate:
    reasons: list[str] = []
    if emergency_stopped(db):
        reasons.append("platform emergency stop is active")
    if live and get_settings().pilot_mode:
        rkind = RECIPIENT_KIND_BY_CHANNEL.get(channel, "EMAIL")
        if not is_allowlisted(db, rkind, recipient):
            reasons.append(f"pilot mode: {rkind.lower()} recipient is not on the internal test allowlist")
    attempt = SendAttempt(kind=kind, ref_id=ref_id, dealership_id=dealership_id, channel=channel, recipient=recipient[:200],
                          live=live, status="BLOCKED" if reasons else "IN_FLIGHT", reasons=reasons)
    db.add(attempt)
    db.flush()
    if reasons:
        audit.record(db, "send.blocked", actor_type="WORKER", dealership_id=dealership_id, entity_type=kind.lower(),
                     entity_id=ref_id, outcome="BLOCKED", channel=channel, live=live, reasons=reasons)
    return Gate(attempt, not reasons, reasons)


def close_attempt(db: Session, attempt: SendAttempt, result: SendResult) -> None:
    if result.ok:
        attempt.status = result.status  # SENT | SIMULATED
        attempt.provider_message_id = result.provider_message_id
    elif result.uncertain:
        attempt.status = "UNCERTAIN"
        attempt.error = result.error
    else:
        attempt.status = "FAILED"
        attempt.error = result.error
    audit.record(db, f"send.{attempt.status.lower()}", actor_type="WORKER", dealership_id=attempt.dealership_id,
                 entity_type=attempt.kind.lower(), entity_id=attempt.ref_id,
                 outcome={"SENT": "OK", "SIMULATED": "SIMULATED"}.get(attempt.status, "FAILED"),
                 external=attempt.live and result.ok, channel=attempt.channel, live=attempt.live,
                 provider_message_id=attempt.provider_message_id, error=attempt.error)


def in_flight_attempt(db: Session, kind: str, ref_id: int) -> SendAttempt | None:
    return db.scalar(select(SendAttempt).where(SendAttempt.kind == kind, SendAttempt.ref_id == ref_id,
                                               SendAttempt.status == "IN_FLIGHT").order_by(SendAttempt.id.desc()))


def check_sending_row(db: Session, kind: str, ref_id: int) -> tuple[str, float | None, SendAttempt | None]:
    """Decide what a job should do when it finds its row already SENDING.

    Returns one of:
      ("recheck", seconds, None)  — no in-flight attempt: the owner just finished (or is mid-update). Never
                                    overwrite; re-read later, when the row will show the owner's outcome.
      ("wait", seconds, attempt)  — attempt younger than the worker lease: the owner may still be sending.
      ("crashed", None, attempt)  — attempt older than the lease: the owner's job lease expired, it is gone.
    Reads here are not one atomic snapshot, so callers must apply "crashed" with ``take_over_crashed``.
    """
    from app.db import utcnow

    att = in_flight_attempt(db, kind, ref_id)
    if att is None:
        return "recheck", 30.0, None
    lease = get_settings().worker_lease_seconds
    age = (utcnow() - att.created_at).total_seconds()
    if age < lease:
        return "wait", lease - age + 5, att
    return "crashed", None, att


def take_over_crashed(db: Session, attempt: SendAttempt, model, status_attr: str, row_id: int) -> bool:
    """Atomically mark a crashed send UNCERTAIN — only if the attempt is still IN_FLIGHT *and* the row is still
    SENDING. If the owner finished in the meantime nothing is changed and False is returned."""
    from sqlalchemy import update

    error = "worker stopped after the send began; provider may have accepted the message"
    with db.begin_nested() as sp:
        took = db.execute(update(SendAttempt).where(SendAttempt.id == attempt.id, SendAttempt.status == "IN_FLIGHT")
                          .values(status="UNCERTAIN", error=error).execution_options(synchronize_session=False)).rowcount
        moved = db.execute(update(model).where(model.id == row_id, getattr(model, status_attr) == "SENDING")
                           .values({status_attr: "UNCERTAIN"}).execution_options(synchronize_session=False)).rowcount if took else 0
        if took != 1 or moved != 1:
            sp.rollback()
            return False
    audit.record(db, "send.uncertain", actor_type="WORKER", dealership_id=attempt.dealership_id,
                 entity_type=attempt.kind.lower(), entity_id=row_id, outcome="FAILED", channel=attempt.channel,
                 error=error)
    return True
