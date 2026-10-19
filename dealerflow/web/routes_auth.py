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
    dest = _landing(user, next)
    if user.mfa_enabled:  # second step: nothing is accessible until the code is verified
        request.session["mfa_pending"] = {"uid": user.id, "t": time.time(), "next": dest}
        audit(db, "auth.password_ok_mfa_pending", actor_id=user.id)
        return RedirectResponse("/login/mfa", status_code=303)
    _start_session(request, db, user, mfa=False)
    return RedirectResponse(dest, status_code=303)


def _landing(user: User, next: str) -> str:
    if user.is_client:  # dealership users only ever see their workspace and the client portal
        home = f"/dealers/{user.client_id}"
        dest = safe_next(next, home)
        return dest if dest.startswith(DEALER_ALLOWED_PREFIXES) else home
    return safe_next(next)


def _start_session(request: Request, db: Session, user: User, *, mfa: bool) -> None:
    request.session.clear()
    request.session.update({"uid": user.id, "t": time.time(), "pv": password_version(user)})
    user.last_login_at = utcnow()
    audit(db, "auth.login", actor_id=user.id, data={"mfa": mfa})


MFA_PENDING_SECONDS = 300


def _pending_user(request: Request, db: Session) -> User | None:
    p = request.session.get("mfa_pending") or {}
    if not p or time.time() - float(p.get("t", 0)) > MFA_PENDING_SECONDS:
        return None
    user = db.get(User, p.get("uid"))
    return user if user and user.active and user.mfa_enabled else None


@router.get("/login/mfa")
def mfa_page(request: Request, db: Session = Depends(get_db)):
    if _pending_user(request, db) is None:
        return RedirectResponse("/login", status_code=303)
    return render(request, "login_mfa.html", None)


@router.post("/login/mfa", dependencies=[Depends(check_csrf)])
def mfa_submit(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    from ..crypto import decrypt_text
    from ..security_mfa import use_recovery_code, verify
    from ..services.rate_limits import hit

    user = _pending_user(request, db)
    if user is None:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    if not hit(f"mfa:user:{user.id}", 10, 900):  # shared across processes
        audit(db, "auth.mfa_blocked", actor_id=user.id)
        return render(request, "login_mfa.html", None, error="Too many attempts. Try again in 15 minutes.")
    ok = False
    counter = verify(decrypt_text(user.mfa_secret_encrypted), code, last_counter=user.mfa_last_counter)
    if counter is not None:
        user.mfa_last_counter, ok = counter, True
    else:
        remaining = use_recovery_code(user.mfa_recovery_hashes, code)
        if remaining is not None:
            user.mfa_recovery_hashes, ok = remaining, True
            audit(db, "auth.mfa_recovery_code_used", actor_id=user.id, data={"remaining": len(remaining)})
    if not ok:
        audit(db, "auth.mfa_failed", actor_id=user.id)
        return render(request, "login_mfa.html", None, error="Invalid code.")
    dest = (request.session.get("mfa_pending") or {}).get("next") or "/"
    _start_session(request, db, user, mfa=True)
    return RedirectResponse(dest, status_code=303)


@router.get("/account/security")
def security_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account_security.html", user, enrolling=False)


@router.post("/account/mfa/start", dependencies=[Depends(check_csrf)])
def mfa_start(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..crypto import encrypt_text
    from ..security_mfa import new_secret, provisioning_uri

    if user.mfa_enabled:
        return RedirectResponse("/account/security", status_code=303)
    secret = new_secret()
    u = db.get(User, user.id)
    u.mfa_pending_secret_encrypted = encrypt_text(secret)
    audit(db, "auth.mfa_enrollment_started", actor_id=user.id)
    return render(request, "account_security.html", u, enrolling=True, secret=secret,
                  uri=provisioning_uri(secret, u.email, "dealerflow"))


@router.post("/account/mfa/confirm", dependencies=[Depends(check_csrf)])
def mfa_confirm(request: Request, code: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..crypto import decrypt_text
    from ..security_mfa import new_recovery_codes, verify

    u = db.get(User, user.id)
    if not u.mfa_pending_secret_encrypted:
        return RedirectResponse("/account/security", status_code=303)
    counter = verify(decrypt_text(u.mfa_pending_secret_encrypted), code, last_counter=None)
    if counter is None:
        return render(request, "account_security.html", u, enrolling=False, error="That code did not match. Start again.")
    codes, hashes = new_recovery_codes()
    u.mfa_secret_encrypted, u.mfa_pending_secret_encrypted = u.mfa_pending_secret_encrypted, None
    u.mfa_enabled, u.mfa_last_counter, u.mfa_recovery_hashes = True, counter, hashes
    db.flush()
    request.session["pv"] = password_version(u)  # other sessions end; this one continues
    audit(db, "auth.mfa_enabled", actor_id=u.id)
    return render(request, "account_security.html", u, enrolling=False, recovery_codes=codes)


@router.post("/account/mfa/disable", dependencies=[Depends(check_csrf)])
def mfa_disable(request: Request, password: str = Form(...), code: str = Form(...), user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    from ..crypto import decrypt_text
    from ..security_mfa import verify

    u = db.get(User, user.id)
    if not u.mfa_enabled:
        return RedirectResponse("/account/security", status_code=303)
    if not verify_password(password, u.password_hash) or verify(decrypt_text(u.mfa_secret_encrypted), code,
                                                                last_counter=u.mfa_last_counter) is None:
        audit(db, "auth.mfa_disable_failed", actor_id=u.id)
        return render(request, "account_security.html", u, enrolling=False, error="Password or code is wrong.")
    u.mfa_enabled, u.mfa_secret_encrypted, u.mfa_recovery_hashes, u.mfa_last_counter = False, None, [], None
    db.flush()
    request.session["pv"] = password_version(u)
    audit(db, "auth.mfa_disabled", actor_id=u.id)
    return RedirectResponse("/account/security", status_code=303)


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request, user: User = Depends(current_user)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
