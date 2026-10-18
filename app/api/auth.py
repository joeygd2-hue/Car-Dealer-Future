from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.api.deps import bearer_token, current_user, get_db, platform_admin
from app.core.audit import audit
from app.core.errors import BadRequest
from app.core.security import create_user, login, logout
from app.core.tenancy import DEALER_ROLES, accessible_clients
from app.core.time import utcnow
from app.models import Client, ClientMembership, User

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def do_login(body: LoginIn, db: Session = Depends(get_db)):
    token, sess, user = login(db, body.email, body.password)
    audit(db, action="auth.login", entity_type="user", entity_id=user.id, actor_type="user", actor_id=user.id)
    return {"token": token, "expires_at": sess.expires_at.isoformat(), "user": _user(user)}


@router.post("/auth/logout")
def do_logout(db: Session = Depends(get_db), token: str | None = Depends(bearer_token)):
    logout(db, token or "")
    return {"ok": True}


def _user(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "platform_role": u.platform_role}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"user": _user(user), "clients": accessible_clients(db, user)}


class ClientIn(BaseModel):
    name: str
    slug: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    timezone: str = "America/Chicago"


@router.get("/clients")
def list_clients(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return accessible_clients(db, user)


@router.post("/clients", status_code=201)
def create_client(body: ClientIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (body.slug or body.name).lower()).strip("-")
    c = Client(name=body.name, slug=slug, city=body.city, state=body.state, postal_code=body.postal_code, timezone=body.timezone)
    db.add(c)
    db.flush()
    audit(db, action="client.created", entity_type="client", entity_id=c.id, client_id=c.id, actor_type="user", actor_id=admin.id)
    return {"id": c.id, "name": c.name, "slug": c.slug}


class UserIn(BaseModel):
    email: EmailStr
    name: str
    password: str
    platform_role: str | None = None


@router.post("/users", status_code=201)
def create_user_route(body: UserIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    if body.platform_role not in (None, "platform_admin", "platform_staff"):
        raise BadRequest("Invalid platform_role")
    u = create_user(db, email=body.email, name=body.name, password=body.password, platform_role=body.platform_role)
    audit(db, action="user.created", entity_type="user", entity_id=u.id, actor_type="user", actor_id=admin.id,
          details={"platform_role": body.platform_role})
    return _user(u)


class MemberIn(BaseModel):
    user_id: str
    role: str


@router.post("/clients/{client_id}/members", status_code=201)
def add_member(client_id: str, body: MemberIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    if body.role not in DEALER_ROLES:
        raise BadRequest(f"role must be one of {', '.join(DEALER_ROLES)}")
    if not db.get(Client, client_id) or not db.get(User, body.user_id):
        raise BadRequest("Unknown client or user")
    m = db.get(ClientMembership, {"client_id": client_id, "user_id": body.user_id})
    if not m:
        m = ClientMembership(client_id=client_id, user_id=body.user_id, role=body.role, created_at=utcnow())
        db.add(m)
    m.role = body.role
    audit(db, action="membership.set", entity_type="client_membership", entity_id=body.user_id, client_id=client_id,
          actor_type="user", actor_id=admin.id, details={"role": body.role})
    return {"client_id": client_id, "user_id": body.user_id, "role": body.role}
