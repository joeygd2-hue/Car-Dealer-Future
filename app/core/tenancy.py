"""Server-side authorisation for dealership-scoped operations.

Route handlers call ``authorize_client`` which verifies membership and role,
then return a ``ClientContext`` carrying a database session bound to that
dealership (see app/db.py). Hiding UI elements is never the control.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Forbidden, NotFound
from app.db import tenant_session
from app.models import Client, ClientMembership, User

DEALER_ROLES = ("dealer_admin", "dealer_reviewer", "dealer_viewer", "agency_operator")

PERMISSIONS: dict[str, tuple[str, ...]] = {
    "view": DEALER_ROLES,
    "import": ("dealer_admin", "dealer_reviewer", "agency_operator"),
    "edit_inventory": ("dealer_admin", "dealer_reviewer", "agency_operator"),
    "generate": ("dealer_admin", "dealer_reviewer", "agency_operator"),
    "edit_listing": ("dealer_admin", "dealer_reviewer", "agency_operator"),
    # agency_operator approval is further gated by dealer settings
    # (review.allow_agency_approval) inside the listing service.
    "approve": ("dealer_admin", "dealer_reviewer", "agency_operator"),
    "publish": ("dealer_admin", "dealer_reviewer", "agency_operator"),
    "manage_settings": ("dealer_admin",),
    "manage_connections": ("dealer_admin",),
}


@dataclass
class Actor:
    type: str  # user | system | job | agent
    id: str | None = None


@dataclass
class ClientContext:
    client_id: str
    role: str
    db: Session
    user: User | None = None
    actor: Actor = field(default_factory=lambda: Actor("system"))


def role_for(db: Session, user: User, client_id: str) -> str | None:
    if not db.get(Client, client_id):
        return None
    if user.platform_role == "platform_admin":
        return "platform_admin"
    m = db.get(ClientMembership, {"client_id": client_id, "user_id": user.id})
    return m.role if m else None


def authorize_client(db: Session, user: User, client_id: str, permission: str = "view") -> ClientContext:
    role = role_for(db, user, client_id)
    if role is None:
        raise NotFound("Dealership not found")  # same answer for "missing" and "not yours"
    if role != "platform_admin":
        allowed = PERMISSIONS[permission]
        if role not in allowed:
            raise Forbidden(f"Your role ({role}) cannot {permission.replace('_', ' ')}")
    return ClientContext(client_id=client_id, role=role, db=tenant_session(client_id), user=user, actor=Actor("user", user.id))


def job_context(client_id: str, job_name: str) -> ClientContext:
    return ClientContext(client_id=client_id, role="system", db=tenant_session(client_id), actor=Actor("job", job_name))


def accessible_clients(db: Session, user: User) -> list[dict]:
    if user.platform_role == "platform_admin":
        return [{"id": c.id, "name": c.name, "slug": c.slug, "role": "platform_admin"}
                for c in db.scalars(select(Client).order_by(Client.name))]
    rows = db.execute(select(Client, ClientMembership.role).join(ClientMembership, ClientMembership.client_id == Client.id)
                      .where(ClientMembership.user_id == user.id).order_by(Client.name))
    return [{"id": c.id, "name": c.name, "slug": c.slug, "role": r} for c, r in rows]


def require_platform(user: User, *, admin: bool = False) -> None:
    """Agency-level features (outreach agent) are for platform staff only."""
    if admin and user.platform_role != "platform_admin":
        raise Forbidden("Platform administrator role required")
    if user.platform_role not in ("platform_admin", "platform_staff"):
        raise Forbidden("Platform staff role required")
