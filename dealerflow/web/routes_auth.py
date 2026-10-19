from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db, utcnow
from ..models import User
from ..security import verify_password
from .deps import DEALER_ALLOWED_PREFIXES, check_csrf, current_user, login_limiter, password_version, render, safe_next

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", None, next=next)


@router.post("/login", dependencies=[Depends(check_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    key = f"{email.lower()}|{request.client.host if request.client else ''}"
    if login_limiter.blocked(key):
        audit(db, "auth.login_blocked", data={"email": email})
        return render(request, "login.html", None, next=next, error="Too many failed attempts. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        login_limiter.fail(key)
        audit(db, "auth.login_failed", data={"email": email})
        return render(request, "login.html", None, next=next, error="Invalid email or password.")
    login_limiter.reset(key)
    request.session.clear()
    request.session.update({"uid": user.id, "t": time.time(), "pv": password_version(user)})
    user.last_login_at = utcnow()
    audit(db, "auth.login", actor_id=user.id)
    if user.is_client:  # dealership users only ever see their workspace and the client portal
        home = f"/dealers/{user.client_id}"
        dest = safe_next(next, home)
        return RedirectResponse(dest if dest.startswith(DEALER_ALLOWED_PREFIXES) else home, status_code=303)
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request, user: User = Depends(current_user)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
