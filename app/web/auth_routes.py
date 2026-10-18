from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.security.auth import SESSION_COOKIE, AuthContext, LoginError, authenticate, create_session, optional_auth, require_auth, revoke_session
from app.services import audit
from app.web.common import TEMPLATES

router = APIRouter()


def _safe_next(url: str | None, default: str) -> str:
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return default


@router.get("/", include_in_schema=False)
def root(ctx: AuthContext | None = Depends(optional_auth)):
    if ctx is None:
        return RedirectResponse("/login", 303)
    return RedirectResponse("/app" if ctx.user.is_staff else "/portal", 303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    return TEMPLATES.TemplateResponse(request, "login.html", {"next": next or "", "error": None, "user": None})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""), db: Session = Depends(get_db)):
    ip = request.client.host if request.client else None
    try:
        user = authenticate(db, email, password)
    except LoginError as exc:
        audit.record(db, "auth.login_failed", details={"email_domain": email.split("@")[-1][:100]}, ip=ip)
        db.commit()
        return TEMPLATES.TemplateResponse(request, "login.html", {"next": next, "error": str(exc), "user": None}, status_code=401)
    token = create_session(db, user, ip, request.headers.get("user-agent"))
    audit.record(db, "auth.login", actor=user, ip=ip)
    db.commit()
    resp = RedirectResponse(_safe_next(next, "/app" if user.is_staff else "/portal"), status_code=303)
    s = get_settings()
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=s.is_production or s.base_url.startswith("https://"), samesite="lax",
                    max_age=s.session_ttl_hours * 3600, path="/")
    return resp


@router.post("/logout")
def logout(request: Request, ctx: AuthContext = Depends(require_auth), db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        revoke_session(db, token)
    audit.record(db, "auth.logout", actor=ctx.user)
    db.commit()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
