"""Support tickets for client issues, failed integrations and change requests."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import SupportTicket, User
from .. import audit

KINDS = ("client_issue", "integration_failure", "change_request", "onboarding", "other")
STATUSES = ("open", "in_progress", "waiting_client", "resolved", "closed")
PRIORITIES = ("low", "normal", "high", "urgent")


def create_ticket(
    db: Session,
    *,
    client_id: int,
    kind: str,
    title: str,
    description: str = "",
    priority: str = "normal",
    actor: User | None = None,
    source: str = "manual",
) -> SupportTicket:
    if kind not in KINDS:
        raise ValueError(f"Unknown ticket kind {kind}")
    if priority not in PRIORITIES:
        raise ValueError(f"Unknown priority {priority}")
    title = title.strip()
    if not title:
        raise ValueError("Title is required")
    t = SupportTicket(
        client_id=client_id,
        kind=kind,
        title=title[:200],
        description=description[:10000],
        priority=priority,
        source=source,
        created_by_id=actor.id if actor else None,
    )
    db.add(t)
    db.flush()
    audit.record(db, "ticket.created", actor=actor, entity=t, details={"kind": kind, "title": t.title})
    return t


def open_system_ticket(
    db: Session, *, client_id: int, kind: str, title: str, description: str, dedupe_key: str, priority: str = "high"
) -> SupportTicket:
    """Create a ticket from an automated failure, unless an unresolved one already exists."""
    existing = db.scalar(
        select(SupportTicket).where(
            SupportTicket.dedupe_key == dedupe_key, SupportTicket.status.notin_(("resolved", "closed"))
        )
    )
    if existing:
        return existing
    t = create_ticket(
        db, client_id=client_id, kind=kind, title=title, description=description, priority=priority, source="system"
    )
    t.dedupe_key = dedupe_key
    return t


def update_status(db: Session, ticket: SupportTicket, status: str, actor: User, resolution: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status {status}")
    old = ticket.status
    ticket.status = status
    if status in ("resolved", "closed"):
        ticket.resolved_at = utcnow()
        ticket.resolution = resolution or ticket.resolution
    audit.record(db, "ticket.status_changed", actor=actor, entity=ticket, details={"from": old, "to": status})
