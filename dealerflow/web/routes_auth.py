"""Login, second factor, logout, account security (password, MFA, sessions) and password reset links."""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import mfa, sessions
from ..audit import audit
from ..config import get_settings
from ..db import get_db, utcnow
from ..models import PasswordResetToken, User
from ..security import hash_password, verify_password
from ..services.customer import rate_limits
from .deps import (
    DEALER_ALLOWED_PREFIXES,
    admin_mfa_required,
    check_csrf,
    current_user,
    flash,
    login_limiter,
    password_version,
    pending_mfa_user,
    render,
    safe_next,
)

router = APIRouter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _landing(user: User, next_: str = "/") -> str:
    if user.is_client:  # dealership users only ever see their workspace and the client portal
        home = f"/dealers/{user.client_id}"
        dest = safe_next(next_, home)
        return dest if dest.startswith(DEALER_ALLOWED_PREFIXES) else home
    return safe_next(next_)


def _start_session(request: Request, db: Session, user: User, *, mfa_verified: bool, next_: str = "/") -> None:
    sessions.destroy(db, request.session.get("sid"))
    sid = sessions.create(db, user, ip=_ip(request), user_agent=request.headers.get("user-agent"), mfa_verified=mfa_verified)
    request.session.clear()  # new CSRF token, no fixation
    request.session.update({"uid": user.id, "sid": sid, "t": time.time(), "pv": password_version(user), "next": next_})


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", None, next=next)


@router.post("/login", dependencies=[Depends(check_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    key = f"{email.lower()}|{_ip(request)}"
    if login_limiter.blocked(db, key) or not rate_limits.try_acquire(db, f"login:ip:{_ip(request)}", 60, 300):
        db.commit()
        audit(db, "auth.login_blocked", data={"email": email})
        return render(request, "login.html", None, next=next, error="Too many failed attempts. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        login_limiter.fail(db, key)
        audit(db, "auth.login_failed", data={"email": email})
        return render(request, "login.html", None, next=next, error="Invalid email or password.")
    login_limiter.reset(db, key)
    _start_session(request, db, user, mfa_verified=not user.mfa_enabled, next_=next)
    if user.mfa_enabled:
        audit(db, "auth.password_ok", actor_id=user.id)
        return RedirectResponse("/login/mfa", status_code=303)
    user.last_login_at = utcnow()
    audit(db, "auth.login", actor_id=user.id)
    return RedirectResponse(_landing(user, next), status_code=303)


@router.get("/login/mfa")
def mfa_page(request: Request, user: User = Depends(pending_mfa_user)):
    return render(request, "login_mfa.html", None)


@router.post("/login/mfa", dependencies=[Depends(check_csrf)])
def mfa_submit(request: Request, code: str = Form(...), user: User = Depends(pending_mfa_user), db: Session = Depends(get_db)):
    if not rate_limits.try_acquire(db, f"mfa:user:{user.id}", 10, 900):
        db.commit()
        return render(request, "login_mfa.html", None, error="Too many attempts. Try again later.")
    ok = False
    secret = mfa.unseal(user.mfa_secret) if user.mfa_enabled and user.mfa_secret else None
    if secret:
        counter = mfa.verify(secret, code, last_counter=user.mfa_last_counter)
        if counter is not None:
            user.mfa_last_counter, ok = counter, True
        else:
            remaining = mfa.use_recovery_code(user.mfa_recovery_hashes or [], code)
            if remaining is not None:
                user.mfa_recovery_hashes, ok = remaining, True
                audit(db, "auth.mfa_recovery_code_used", actor_id=user.id, data={"remaining": len(remaining)})
    if not ok:
        audit(db, "auth.mfa_failed", actor_id=user.id, data={"ip": _ip(request)})
        db.commit()
        return render(request, "login_mfa.html", None, error="Invalid code.")
    next_ = request.session.get("next", "/")
    _start_session(request, db, user, mfa_verified=True, next_=next_)  # rotate: half-authenticated id is discarded
    user.last_login_at = utcnow()
    audit(db, "auth.login", actor_id=user.id, data={"mfa": True})
    return RedirectResponse(_landing(user, next_), status_code=303)


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sessions.destroy(db, request.session.get("sid"))
    audit(db, "auth.logout", actor_id=user.id)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- account security -----------------------------------------------------------------------------------------


@router.get("/account/security")
def security_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account_security.html", user, mfa_required=admin_mfa_required() and user.client_id is None and user.role == "admin")


@router.post("/account/password", dependencies=[Depends(check_csrf)])
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(current_password, user.password_hash):
        flash(request, "Current password is incorrect.", "error")
        return RedirectResponse("/account/security", status_code=303)
    try:
        user.password_hash = hash_password(new_password)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/account/security", status_code=303)
    user.password_changed_at = utcnow()
    ended = sessions.end_all(db, user.id, except_sid=request.session.get("sid"))
    request.session["pv"] = password_version(user)
    audit(db, "auth.password_changed", actor_id=user.id, data={"other_sessions_ended": ended})
    flash(request, "Password changed. Your other sessions were signed out.")
    return RedirectResponse("/account/security", status_code=303)


@router.post("/account/sessions/end-others", dependencies=[Depends(check_csrf)])
def end_other_sessions(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ended = sessions.end_all(db, user.id, except_sid=request.session.get("sid"))
    audit(db, "auth.sessions_ended", actor_id=user.id, data={"count": ended})
    flash(request, f"Signed out {ended} other session(s).")
    return RedirectResponse("/account/security", status_code=303)


@router.post("/account/mfa/start", dependencies=[Depends(check_csrf)])
def mfa_start(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    secret = mfa.new_secret()
    user.mfa_pending_secret = mfa.seal(secret)
    db.commit()
    return render(request, "account_security.html", user, pending_secret=secret,
                  uri=mfa.provisioning_uri(secret, user.email, get_settings().business_name or "dealerflow"))


@router.post("/account/mfa/confirm", dependencies=[Depends(check_csrf)])
def mfa_confirm(request: Request, code: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    secret = mfa.unseal(user.mfa_pending_secret) if user.mfa_pending_secret else None
    if not secret:
        flash(request, "Start MFA setup first.", "error")
        return RedirectResponse("/account/security", status_code=303)
    counter = mfa.verify(secret, code, last_counter=None)
    if counter is None:
        flash(request, "That code didn't match. Check your authenticator's clock and try again.", "error")
        return RedirectResponse("/account/security", status_code=303)
    codes, hashes = mfa.new_recovery_codes()
    user.mfa_secret, user.mfa_pending_secret = mfa.seal(secret), None
    user.mfa_enabled, user.mfa_last_counter, user.mfa_recovery_hashes = True, counter, hashes
    sessions.end_all(db, user.id, except_sid=request.session.get("sid"))
    audit(db, "auth.mfa_enabled", actor_id=user.id)
    db.commit()
    return render(request, "account_security.html", user, recovery_codes=codes)  # shown once, never stored in plaintext


@router.post("/account/mfa/disable", dependencies=[Depends(check_csrf)])
def mfa_disable(request: Request, password: str = Form(...), code: str = Form(...), user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    if admin_mfa_required() and user.client_id is None and user.role == "admin":
        flash(request, "MFA is required for agency admins.", "error")
        return RedirectResponse("/account/security", status_code=303)
    secret = mfa.unseal(user.mfa_secret) if user.mfa_secret else None
    if not (verify_password(password, user.password_hash) and secret and mfa.verify(secret, code, last_counter=user.mfa_last_counter) is not None):
        flash(request, "Password or code incorrect.", "error")
        return RedirectResponse("/account/security", status_code=303)
    user.mfa_enabled, user.mfa_secret, user.mfa_recovery_hashes = False, None, None
    audit(db, "auth.mfa_disabled", actor_id=user.id)
    flash(request, "Two-factor authentication turned off.")
    return RedirectResponse("/account/security", status_code=303)


# --- password reset / setup links -----------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_reset_token(db: Session, user: User, *, created_by_id: int | None, purpose: str = "RESET") -> str:
    token = secrets.token_urlsafe(32)
    minutes = get_settings().password_reset_ttl_minutes if purpose == "RESET" else 72 * 60
    db.add(PasswordResetToken(token_hash=_token_hash(token), user_id=user.id, purpose=purpose, created_by_id=created_by_id,
                              expires_at=utcnow() + timedelta(minutes=minutes)))
    audit(db, f"auth.{purpose.lower()}_link_issued", actor_id=created_by_id, entity_type="user", entity_id=user.id)
    return f"{get_settings().base_url.rstrip('/')}/password/reset/{token}"


def _send_reset_email(db: Session, user: User, link: str) -> str:
    """Transactional email through the configured provider, gated like every other send (emergency stop, pilot)."""
    from ..audit import track_integration
    from ..integrations.email import OutgoingEmail, get_email_provider
    from ..services import pilot

    s = get_settings()
    provider = get_email_provider()
    if not provider.simulated:
        blocker = pilot.live_send_blocker(db, "EMAIL", user.email)
        if blocker:
            audit(db, "auth.reset_email_blocked", entity_type="user", entity_id=user.id, data={"reason": blocker})
            return "blocked"
    with track_integration(db, provider.name, "password_reset_email", simulated=provider.simulated):
        provider.send(OutgoingEmail(to=user.email, subject="Reset your dealerflow password",
                                    body=f"Use this link within {s.password_reset_ttl_minutes} minutes to set a new password:\n{link}\n\n"
                                         "If you didn't ask for this, you can ignore this email.",
                                    from_name=s.sender_name or "dealerflow", from_email=s.sender_email or "no-reply@dealerflow.invalid"))
    return "simulated" if provider.simulated else "sent"


@router.get("/password/forgot")
def forgot_page(request: Request):
    return render(request, "password_forgot.html", None)


@router.post("/password/forgot", dependencies=[Depends(check_csrf)])
def forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    em = email.strip().lower()[:200]
    allowed = (rate_limits.try_acquire(db, f"reset:ip:{_ip(request)}", 10, 3600)
               and rate_limits.try_acquire(db, f"reset:acct:{em}", 3, 3600))
    user = db.scalar(select(User).where(User.email == em, User.active.is_(True))) if allowed else None
    if user:
        try:
            _send_reset_email(db, user, issue_reset_token(db, user, created_by_id=None))
        except Exception:  # noqa: BLE001 - never reveal whether the account exists; failure is in integration_log
            pass
    db.commit()
    return render(request, "password_forgot.html", None, sent=True)  # same answer whether or not the account exists


def _valid_token(db: Session, token: str) -> PasswordResetToken | None:
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == _token_hash(token)))
    if row is None or row.used_at is not None or row.expires_at < utcnow():
        return None
    return row


@router.get("/password/reset/{token}")
def reset_page(token: str, request: Request, db: Session = Depends(get_db)):
    return render(request, "password_reset.html", None, valid=_valid_token(db, token) is not None, token=token)


@router.post("/password/reset/{token}", dependencies=[Depends(check_csrf)])
def reset(token: str, request: Request, new_password: str = Form(...), confirm_password: str = Form(...),
          db: Session = Depends(get_db)):
    row = _valid_token(db, token)
    if row is None:
        return render(request, "password_reset.html", None, valid=False, token=token)
    if new_password != confirm_password:
        return render(request, "password_reset.html", None, valid=True, token=token, error="Passwords don't match.")
    user = db.get(User, row.user_id)
    try:
        user.password_hash = hash_password(new_password)
    except ValueError as exc:
        return render(request, "password_reset.html", None, valid=True, token=token, error=str(exc))
    user.password_changed_at = row.used_at = utcnow()
    ended = sessions.end_all(db, user.id)
    audit(db, "auth.password_reset", actor_id=user.id, data={"purpose": row.purpose, "sessions_ended": ended})
    flash(request, "Password updated. Please sign in.")
    return RedirectResponse("/login", status_code=303)
