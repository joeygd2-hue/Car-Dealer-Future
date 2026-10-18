from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.security.auth import SESSION_COOKIE, authenticate, create_session, destroy_session
from app.services import audit
from app.web.deps import RateLimiter, render

router = APIRouter()
_login_limiter = RateLimiter(10, 300)


@router.get("/login")
def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if not _login_limiter.allow(f"{ip}:{email.lower()}"):
        return render(request, "login.html", error="Too many attempts. Try again in a few minutes.")
    user = authenticate(db, email, password)
    if not user:
        audit.record(db, "auth.login_failed", actor_type="USER", outcome="FAILED", email=email.lower()[:200], ip=ip)
        db.commit()
        return render(request, "login.html", error="Invalid email or password.")
    token = create_session(db, user, ip=ip, user_agent=request.headers.get("user-agent"))
    audit.record(db, "auth.login", actor_type="USER", actor_user_id=user.id, ip=ip)
    db.commit()
    resp = RedirectResponse("/admin" if user.is_platform_admin else "/portal", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=get_settings().is_production,
                    max_age=get_settings().session_ttl_hours * 3600)
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    destroy_session(db, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
