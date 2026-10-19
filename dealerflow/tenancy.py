"""Dealership (client) tenancy: scoped sessions and access rules for Phase 2+.

Isolation is enforced in three layers:

1. Database: composite (client_id, parent_id) foreign keys (``models_dealer``).
2. ORM: ``tenant_session(client_id)`` adds ``client_id = :tenant`` to every SELECT/UPDATE/DELETE on
   tenant models and refuses to flush a row for another client or a changed ``client_id``.
3. Web: routes obtain a ``ClientContext`` only via ``authorize_client`` (server-side role checks).
   Unknown client and "not your client" both return 404.

Who can do what:

- Staff (``users.client_id IS NULL``) can open any client workspace. viewer = read only;
  rep = import, generate, edit, publish; manager/admin = also settings and channel connections.
  Staff approve listings only if that dealership's settings allow agency approval.
- Dealership users (``users.client_id`` set) see only their own client and nothing else in the app.
  dealer_viewer = read only; dealer_reviewer = edit, approve, publish, import; dealer_admin = also settings.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, with_loader_criteria

from .db import session_factory
from .models import Client, User
from .models_dealer import TenantMixin
from .security import role_at_least

PERMISSIONS: dict[str, dict[str, tuple[str, ...]]] = {
    #               staff roles allowed                              dealership roles allowed
    "view": {"staff": ("viewer", "rep", "manager", "admin"), "dealer": ("dealer_admin", "dealer_reviewer", "dealer_viewer")},
    "import": {"staff": ("rep", "manager", "admin"), "dealer": ("dealer_admin", "dealer_reviewer")},
    "edit": {"staff": ("rep", "manager", "admin"), "dealer": ("dealer_admin", "dealer_reviewer")},
    "approve": {"staff": ("rep", "manager", "admin"), "dealer": ("dealer_admin", "dealer_reviewer")},
    "publish": {"staff": ("rep", "manager", "admin"), "dealer": ("dealer_admin", "dealer_reviewer")},
    # Phase 3: reply to customers, assign, take over, resolve escalations, handle appointment requests.
    "respond": {"staff": ("rep", "manager", "admin"), "dealer": ("dealer_admin", "dealer_reviewer")},
    "manage": {"staff": ("manager", "admin"), "dealer": ("dealer_admin",)},
}


class TenantViolation(Exception):
    pass


@dataclass
class ClientContext:
    client: Client
    db: Session
    user: User | None
    is_staff: bool
    role: str

    @property
    def client_id(self) -> int:
        return self.client.id

    @property
    def actor_id(self) -> int | None:
        return self.user.id if self.user else None

    @property
    def actor_label(self) -> str:
        return self.user.email if self.user else "system"

    def can(self, permission: str) -> bool:
        return self.user is None or permission_ok(self.user, permission)

    def close(self) -> None:
        self.db.close()


def permission_ok(user: User, permission: str) -> bool:
    rule = PERMISSIONS[permission]
    if user.client_id is None:
        return user.role in rule["staff"]
    return (user.client_role or "") in rule["dealer"]


def tenant_session(client_id: int) -> Session:
    s = session_factory()()
    s.info["tenant_id"] = client_id
    return s


def authorize_client(db: Session, user: User, client_id: int, permission: str = "view") -> ClientContext:
    client = db.get(Client, client_id)
    if client is None or (user.client_id is not None and user.client_id != client_id):
        raise HTTPException(404, "Dealership not found")
    if not permission_ok(user, permission):
        raise HTTPException(403, f"Your role cannot {permission} in this dealership workspace")
    s = tenant_session(client_id)
    return ClientContext(client=s.get(Client, client_id), db=s, user=s.get(User, user.id), is_staff=user.client_id is None,
                         role=user.role if user.client_id is None else (user.client_role or ""))


def system_context(client_id: int) -> ClientContext:
    """For background jobs acting on behalf of the platform for one client."""
    s = tenant_session(client_id)
    client = s.get(Client, client_id)
    if client is None:
        s.close()
        raise ValueError(f"client {client_id} not found")
    return ClientContext(client=client, db=s, user=None, is_staff=True, role="system")


def accessible_clients(db: Session, user: User) -> list[Client]:
    from sqlalchemy import select

    if user.client_id is not None:
        c = db.get(Client, user.client_id)
        return [c] if c else []
    if not role_at_least(user.role, "viewer"):
        return []
    return list(db.scalars(select(Client).order_by(Client.name)))


@event.listens_for(Session, "do_orm_execute")
def _scope_queries(state):
    tenant = state.session.info.get("tenant_id")
    if tenant is not None and not state.is_column_load and not state.is_relationship_load:
        state.statement = state.statement.options(
            with_loader_criteria(TenantMixin, lambda cls: cls.client_id == tenant, include_aliases=True))


@event.listens_for(Session, "before_flush")
def _check_writes(session, _ctx, _instances):
    tenant = session.info.get("tenant_id")
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if not isinstance(obj, TenantMixin):
            continue
        hist = inspect(obj).attrs.client_id.history
        if obj not in session.new and hist.deleted and hist.added and hist.deleted[0] != hist.added[0]:
            raise TenantViolation("client_id is immutable")
        if tenant is not None and obj.client_id != tenant:
            raise TenantViolation(f"{type(obj).__name__} belongs to a different dealership")
