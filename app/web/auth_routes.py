"""Login (+ TOTP MFA), logout, account security (password change, MFA enrollment) and password reset."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import Notification, PasswordResetToken, User
from app.security import mfa
from app.security.auth import (
    SESSION_COOKIE,
    AuthContext,
    authenticate,
    create_session,
    destroy_session,
    get_auth,
    get_auth_pending_mfa,
    invalidate_sessions,
    session_from_token,
    verify_csrf,
)
from app.security.crypto import decrypt_json, encrypt_json
from app.security.passwords import hash_password, verify_password
from app.services import audit, jobs, rate_limits
from app.web.deps import redirect, render

router = APIRouter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_cookie(resp, token: str) -> None:
    s = get_settings()
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=s.is_production,
                    max_age=s.session_ttl_hours * 3600)


def _landing(user: User) -> str:
    return "/admin" if user.is_platform_admin else "/portal"


@router.get("/login")
def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    ip, em = _ip(request), email.strip().lower()[:200]
    # Distributed throttles (shared by all web processes): per IP and per account.
    if not (rate_limits.try_acquire(db, f"login:ip:{ip}", 30, 300) and rate_limits.try_acquire(db, f"login:acct:{em}", 10, 900)):
        db.commit()
        audit.record(db, "auth.login_throttled", actor_type="USER", outcome="BLOCKED", email=em, ip=ip)
        db.commit()
        return render(request, "login.html", error="Too many attempts. Try again in a few minutes.")
    user = authenticate(db, em, password)
    if not user:
        audit.record(db, "auth.login_failed", actor_type="USER", outcome="FAILED", email=em, ip=ip)
        db.commit()
        return render(request, "login.html", error="Invalid email or password.")
    token = create_session(db, user, ip=ip, user_agent=request.headers.get("user-agent"))
    audit.record(db, "auth.password_ok" if user.mfa_enabled else "auth.login", actor_type="USER", actor_user_id=user.id, ip=ip)
    db.commit()
    resp = RedirectResponse("/login/mfa" if user.mfa_enabled else _landing(user), status_code=303)
    _set_cookie(resp, token)
    return resp


@router.get("/login/mfa")
def mfa_form(request: Request, auth: AuthContext = Depends(get_auth_pending_mfa)):
    return render(request, "mfa_challenge.html")


@router.post("/login/mfa")
def mfa_submit(request: Request, code: str = Form(...), auth: AuthContext = Depends(get_auth_pending_mfa),
               db: Session = Depends(get_db)):
    user = auth.user
    if not rate_limits.try_acquire(db, f"mfa:user:{user.id}", 10, 900):
        db.commit()
        return render(request, "mfa_challenge.html", error="Too many attempts. Try again later.")
    ok = False
    if user.mfa_enabled and user.mfa_secret_encrypted:
        secret = decrypt_json(user.mfa_secret_encrypted)["secret"]
        counter = mfa.verify(secret, code, last_counter=user.mfa_last_counter)
        if counter is not None:
            user.mfa_last_counter, ok = counter, True
        else:
            remaining = mfa.use_recovery_code(user.mfa_recovery_hashes, code)
            if remaining is not None:
                user.mfa_recovery_hashes, ok = remaining, True
                audit.record(db, "auth.mfa_recovery_code_used", actor_type="USER", actor_user_id=user.id)
    if not ok:
        audit.record(db, "auth.mfa_failed", actor_type="USER", actor_user_id=user.id, outcome="FAILED", ip=_ip(request))
        db.commit()
        return render(request, "mfa_challenge.html", error="Invalid code.")
    # Rotate: drop the half-authenticated session and issue a fresh fully-verified one.
    destroy_session(db, request.cookies.get(SESSION_COOKIE))
    token = create_session(db, user, ip=_ip(request), user_agent=request.headers.get("user-agent"), mfa_verified=True)
    audit.record(db, "auth.login", actor_type="USER", actor_user_id=user.id, mfa=True, ip=_ip(request))
    db.commit()
    resp = RedirectResponse(_landing(user), status_code=303)
    _set_cookie(resp, token)
    return resp


@router.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    sess = session_from_token(db, token)
    if sess is not None:
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf_token", "")), sess.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
        audit.record(db, "auth.logout", actor_type="USER", actor_user_id=sess.user_id)
        destroy_session(db, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ------------------------------------------------------------------------------ account security


@router.get("/account/security")
def security_page(request: Request, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    return render(request, "account_security.html", db, mfa_required=get_settings().admin_mfa_required and auth.user.is_platform_admin)


@router.post("/account/password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                    auth: AuthContext = Depends(verify_csrf), db: Session = Depends(get_db)):
    user = db.get(User, auth.user.id)
    if not verify_password(current_password, user.password_hash):
        return redirect("/account/security", err="Current password is incorrect.")
    try:
        user.password_hash = hash_password(new_password)
    except ValueError as exc:
        return redirect("/account/security", err=str(exc))
    user.password_changed_at = utcnow()
    n = invalidate_sessions(db, user.id, except_token=request.cookies.get(SESSION_COOKIE))
    audit.record(db, "auth.password_changed", actor_type="USER", actor_user_id=user.id, other_sessions_ended=n)
    db.commit()
    return redirect("/account/security", msg="Password changed. Your other sessions were signed out.")


@router.post("/account/mfa/start")
def mfa_start(request: Request, auth: AuthContext = Depends(verify_csrf), db: Session = Depends(get_db)):
    user = db.get(User, auth.user.id)
    secret = mfa.new_secret()
    user.mfa_pending_secret_encrypted = encrypt_json({"secret": secret})
    db.commit()
    return render(request, "account_security.html", db, pending_secret=secret,
                  uri=mfa.provisioning_uri(secret, user.email, get_settings().app_name),
                  mfa_required=get_settings().admin_mfa_required and user.is_platform_admin)


@router.post("/account/mfa/confirm")
def mfa_confirm(request: Request, code: str = Form(...), auth: AuthContext = Depends(verify_csrf), db: Session = Depends(get_db)):
    user = db.get(User, auth.user.id)
    if not user.mfa_pending_secret_encrypted:
        return redirect("/account/security", err="Start MFA setup first.")
    secret = decrypt_json(user.mfa_pending_secret_encrypted)["secret"]
    counter = mfa.verify(secret, code, last_counter=None)
    if counter is None:
        return redirect("/account/security", err="That code didn't match. Check your authenticator's clock and try again.")
    codes, hashes = mfa.new_recovery_codes()
    user.mfa_secret_encrypted, user.mfa_pending_secret_encrypted = encrypt_json({"secret": secret}), None
    user.mfa_enabled, user.mfa_last_counter, user.mfa_recovery_hashes = True, counter, hashes
    invalidate_sessions(db, user.id, except_token=request.cookies.get(SESSION_COOKIE))
    audit.record(db, "auth.mfa_enabled", actor_type="USER", actor_user_id=user.id)
    db.commit()
    # Rendered once, never stored in plaintext or put in a URL.
    return render(request, "account_security.html", db, recovery_codes=codes, mfa_required=False)


@router.post("/account/mfa/disable")
def mfa_disable(request: Request, password: str = Form(...), code: str = Form(...), auth: AuthContext = Depends(verify_csrf),
                db: Session = Depends(get_db)):
    user = db.get(User, auth.user.id)
    if get_settings().admin_mfa_required and user.is_platform_admin:
        return redirect("/account/security", err="MFA is required for platform administrators.")
    secret = decrypt_json(user.mfa_secret_encrypted)["secret"] if user.mfa_secret_encrypted else None
    if not (verify_password(password, user.password_hash) and secret
            and mfa.verify(secret, code, last_counter=user.mfa_last_counter) is not None):
        return redirect("/account/security", err="Password or code incorrect.")
    user.mfa_enabled, user.mfa_secret_encrypted, user.mfa_recovery_hashes = False, None, []
    audit.record(db, "auth.mfa_disabled", actor_type="USER", actor_user_id=user.id)
    db.commit()
    return redirect("/account/security", msg="MFA disabled.")


# ------------------------------------------------------------------------------ password reset


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_reset_token(db: Session, user: User, *, created_by_id: int | None, purpose: str = "RESET") -> str:
    token = secrets.token_urlsafe(32)
    ttl = get_settings().password_reset_ttl_minutes if purpose == "RESET" else 72 * 60
    db.add(PasswordResetToken(token_hash=_hash(token), user_id=user.id, purpose=purpose, created_by_id=created_by_id,
                              expires_at=utcnow() + timedelta(minutes=ttl)))
    audit.record(db, f"auth.{purpose.lower()}_link_issued", actor_type="USER" if created_by_id else "SYSTEM",
                 actor_user_id=created_by_id, entity_type="user", entity_id=user.id)
    return token


@router.get("/password/forgot")
def forgot_form(request: Request):
    return render(request, "password_forgot.html")


@router.post("/password/forgot")
def forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    em = email.strip().lower()[:200]
    allowed = rate_limits.try_acquire(db, f"reset:ip:{_ip(request)}", 10, 3600) and rate_limits.try_acquire(db, f"reset:acct:{em}", 3, 3600)
    user = db.scalar(select(User).where(User.email == em, User.is_active.is_(True))) if allowed else None
    if user:
        token = issue_reset_token(db, user, created_by_id=None)
        link = f"{get_settings().base_url.rstrip('/')}/password/reset/{token}"
        n = Notification(user_id=user.id, recipient_address=user.email, channel="EMAIL", kind="SYSTEM",
                         subject="Reset your password",
                         body=f"Use this link within {get_settings().password_reset_ttl_minutes} minutes to set a new password:\n{link}\n\n"
                              "If you didn't ask for this, ignore this email.", status="PENDING")
        db.add(n)
        db.flush()
        jobs.enqueue(db, "notification.send_email", {"notification_id": n.id}, idempotency_key=f"notify-email:{n.id}")
    db.commit()
    # Same response whether or not the account exists (no account enumeration).
    return render(request, "password_forgot.html", sent=True)


def _valid_token(db: Session, token: str) -> PasswordResetToken | None:
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash(token)))
    if row is None or row.used_at is not None or row.expires_at < utcnow():
        return None
    return row


@router.get("/password/reset/{token}")
def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    return render(request, "password_reset.html", valid=_valid_token(db, token) is not None, token=token)


@router.post("/password/reset/{token}")
def reset(token: str, request: Request, new_password: str = Form(...), confirm_password: str = Form(...),
          db: Session = Depends(get_db)):
    row = _valid_token(db, token)
    if row is None:
        return render(request, "password_reset.html", valid=False, token=token)
    if new_password != confirm_password:
        return render(request, "password_reset.html", valid=True, token=token, error="Passwords don't match.")
    user = db.get(User, row.user_id)
    try:
        user.password_hash = hash_password(new_password)
    except ValueError as exc:
        return render(request, "password_reset.html", valid=True, token=token, error=str(exc))
    user.password_changed_at = row.used_at = utcnow()
    n = invalidate_sessions(db, user.id)
    audit.record(db, "auth.password_reset", actor_type="USER", actor_user_id=user.id, purpose=row.purpose, sessions_ended=n)
    db.commit()
    return redirect("/login", msg="Password updated. Please sign in.")
