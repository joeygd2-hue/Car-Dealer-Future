"""Single checkpoint for every customer message and staff notification email that could leave the platform.

Every send path calls ``open_attempt`` immediately before the provider call. It enforces the platform-wide
emergency stop (Phase 1's agent emergency stop is the one switch for every external action) and the global
customer-automation pause for automated sends, and writes a ``SendAttempt`` row (the outbound ledger) that the
caller commits BEFORE calling the provider. ``close_attempt`` records the outcome. An attempt left IN_FLIGHT means
"the provider may have accepted it" and is never retried automatically (see ``check_sending_row`` /
``take_over_crashed``).

Phase 3's pilot allowlist is not ported: no live customer-messaging adapter exists in this build, so every send
is ``live=False`` (SIMULATED).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...db import utcnow
from ...jobs import STALE_LOCK
from ...models import AgentState, SendAttempt
from .channels import SendResult
from .common import record

# A worker that has held a job longer than this is considered dead (the job queue releases its lock).
WORKER_LEASE = STALE_LOCK


def emergency_stopped(db: Session) -> bool:
    st = db.get(AgentState, 1)
    return st is not None and st.status == "emergency_stopped"


@dataclass
class Gate:
    attempt: SendAttempt
    allowed: bool
    reasons: list[str]


def open_attempt(db: Session, *, kind: str, ref_id: int, channel: str, recipient: str, live: bool,
                 client_id: int | None = None) -> Gate:
    reasons: list[str] = []
    if emergency_stopped(db):
        reasons.append("platform emergency stop is active")
    attempt = SendAttempt(kind=kind, ref_id=ref_id, client_id=client_id, channel=channel, recipient=(recipient or "")[:200],
                          live=live, status="BLOCKED" if reasons else "IN_FLIGHT", reasons=reasons)
    db.add(attempt)
    db.flush()
    if reasons:
        record(db, "send.blocked", client_id=client_id, entity_type=kind.lower(), entity_id=ref_id, outcome="BLOCKED",
               channel=channel, live=live, reasons=reasons)
    return Gate(attempt, not reasons, reasons)


def close_attempt(db: Session, attempt: SendAttempt, result: SendResult) -> None:
    if result.ok:
        attempt.status = result.status  # SENT | SIMULATED
        attempt.provider_message_id = result.provider_message_id
    elif result.uncertain:
        attempt.status, attempt.error = "UNCERTAIN", result.error
    else:
        attempt.status, attempt.error = "FAILED", result.error
    record(db, f"send.{attempt.status.lower()}", client_id=attempt.client_id, entity_type=attempt.kind.lower(),
           entity_id=attempt.ref_id, outcome={"SENT": "OK", "SIMULATED": "SIMULATED"}.get(attempt.status, "FAILED"),
           channel=attempt.channel, live=attempt.live, provider_message_id=attempt.provider_message_id, error=attempt.error)


def in_flight_attempt(db: Session, kind: str, ref_id: int) -> SendAttempt | None:
    return db.scalar(select(SendAttempt).where(SendAttempt.kind == kind, SendAttempt.ref_id == ref_id,
                                               SendAttempt.status == "IN_FLIGHT").order_by(SendAttempt.id.desc()))


def check_sending_row(db: Session, kind: str, ref_id: int) -> tuple[str, float, SendAttempt | None]:
    """Decide what a job should do when it finds its row already SENDING.

    ("recheck", s, None)  — no in-flight attempt: the owner just finished (or is mid-update). Never overwrite.
    ("wait", s, attempt)  — attempt younger than the worker lease: the owner may still be sending.
    ("crashed", 0, attempt) — attempt older than the lease: the owner is gone. Apply with ``take_over_crashed``.
    """
    att = in_flight_attempt(db, kind, ref_id)
    if att is None:
        return "recheck", 30.0, None
    age = (utcnow() - att.created_at).total_seconds()
    lease = WORKER_LEASE.total_seconds()
    if age < lease:
        return "wait", lease - age + 5, att
    return "crashed", 0.0, att


def take_over_crashed(db: Session, attempt: SendAttempt, model, status_attr: str, row_id: int) -> bool:
    """Atomically mark a crashed send UNCERTAIN, only if the attempt is still IN_FLIGHT *and* the row still SENDING.
    If the owner finished in the meantime nothing changes and False is returned."""
    error = "worker stopped after the send began; provider may have accepted the message"
    with db.begin_nested() as sp:
        took = db.execute(update(SendAttempt).where(SendAttempt.id == attempt.id, SendAttempt.status == "IN_FLIGHT")
                          .values(status="UNCERTAIN", error=error).execution_options(synchronize_session=False)).rowcount
        moved = db.execute(update(model).where(model.id == row_id, getattr(model, status_attr) == "SENDING")
                           .values({status_attr: "UNCERTAIN"}).execution_options(synchronize_session=False)).rowcount if took else 0
        if took != 1 or moved != 1:
            sp.rollback()
            return False
    record(db, "send.uncertain", client_id=attempt.client_id, entity_type=attempt.kind.lower(), entity_id=row_id,
           outcome="FAILED", channel=attempt.channel, error=error)
    return True


def defer_seconds(seconds: float) -> timedelta:
    return timedelta(seconds=max(5.0, seconds))
