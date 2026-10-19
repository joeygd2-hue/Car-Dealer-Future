"""Client (tenant) isolation helpers and conversion of a won prospect into a client.

Rule: every query for client-owned data made on behalf of a client user goes through
``scope()`` / ``owned_or_404()``, which add ``client_id = user.client_id``. Portal routes never
take a client id from the request. Staff users (viewer and above) are not restricted by client.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import utcnow
from ..models import Client, Prospect, Suppression, User
from . import onboarding


def scope(stmt: Any, model: Any, user: User) -> Any:
    if not user.is_client:
        return stmt
    if user.client_id is None:  # a client user without a client sees nothing
        return stmt.where(False)
    return stmt.where(model.client_id == user.client_id)


def owned_or_404(db: Session, model: Any, obj_id: int, user: User) -> Any:
    """Fetch a client-owned row; 404 (not 403) when it is outside the caller's scope, to avoid leaking existence."""
    obj = db.scalar(scope(select(model).where(model.id == obj_id), model, user))
    if obj is None:
        raise HTTPException(404, "Not found")
    return obj


def create_client(db: Session, *, name: str, actor: User | None, prospect: Prospect | None = None, **fields) -> Client:
    if prospect is not None:
        existing = db.scalar(select(Client).where(Client.prospect_id == prospect.id))
        if existing:
            return existing
        for k in ("website", "address_line", "city", "state", "postal_code", "phone"):
            fields.setdefault(k, getattr(prospect, k, None))
        fields.setdefault("contact_email", prospect.email)
    c = Client(name=name.strip()[:255], prospect_id=prospect.id if prospect else None, **fields)
    db.add(c)
    db.flush()
    onboarding.ensure_checklist(db, c)
    if prospect is not None:
        # A client must never receive prospecting outreach again: stop sequences and suppress the address.
        from ..models import Enrollment

        for enr in db.scalars(select(Enrollment).where(Enrollment.prospect_id == prospect.id, Enrollment.status == "active")):
            enr.status, enr.stop_reason, enr.stopped_at = "stopped", "converted to client", utcnow()
        if prospect.email and not db.scalar(select(Suppression).where(Suppression.value == prospect.email.lower())):
            db.add(Suppression(value=prospect.email.lower(), kind="email", reason="existing_client", source=f"client:{c.id}"))
    audit.record(db, "client.created", actor=actor, entity=c, details={"from_prospect": prospect.id if prospect else None})
    return c
