"""Client creation (the one path for /dealers, /clients and the API) and portal query scoping.

Rule: every Phase 4 portal query for client-owned data goes through ``scope()`` / ``owned_or_404()``,
which add ``client_id = user.client_id``. Portal routes never take a client id from the request. Staff
users are not restricted by client. Phase 2 dealership pages use ``dealerflow.tenancy`` instead.
"""

from __future__ import annotations

import re
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


def unique_slug(db: Session, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:100] or "client"
    slug, n = base, 2
    while db.scalar(select(Client.id).where(Client.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_client(db: Session, *, name: str, actor: User | None, prospect: Prospect | None = None, **fields) -> Client:
    """New clients start in ``onboarding``: scheduled automations skip them until the checklist activates them."""
    if prospect is not None:
        existing = db.scalar(select(Client).where(Client.prospect_id == prospect.id))
        if existing:
            return existing
        for k in ("website", "address_line", "city", "state", "postal_code", "phone"):
            fields.setdefault(k, getattr(prospect, k, None))
        fields.setdefault("contact_email", prospect.email)
        fields.setdefault("simulated", bool(prospect.simulated))
    fields.setdefault("status", "onboarding")
    name = name.strip()[:255]
    c = Client(name=name, slug=unique_slug(db, name), prospect_id=prospect.id if prospect else None, **fields)
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
