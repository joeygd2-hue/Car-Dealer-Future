"""Pilot console (/admin/pilot, agency admins): one place to see and control everything that could leave the platform."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import audit as audit_mod
from ..config import get_settings
from ..db import get_db
from ..integrations import customer_channels as channels
from ..jobs import enqueue
from ..models import Job, OutreachMessage, PilotRecipient, User
from ..models_customer import CustomerMessage, DealerNotification, SendAttempt
from ..models_ops import IntegrationConnection
from ..services import agent, pilot
from ..services.customer import controls
from ..services.customer.outbound import reconcile_uncertain
from .deps import check_csrf, flash, render, require

router = APIRouter(prefix="/admin/pilot", dependencies=[Depends(check_csrf)])


def _go(request: Request, msg: str, kind: str = "info") -> RedirectResponse:
    flash(request, msg, kind)
    return RedirectResponse("/admin/pilot", status_code=303)


@router.get("")
def console(request: Request, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    s = get_settings()
    st = agent.state(db)
    ctx = dict(
        switches=[("DF_PILOT_MODE", s.pilot_mode, "Live sends only to the allowlist below"),
                  ("DF_LIVE_CUSTOMER_MESSAGING_ENABLED", s.live_customer_messaging_enabled, "Real replies to car buyers"),
                  ("DF_OUTREACH_SENDING_ENABLED", s.outreach_sending_enabled, "Real outreach email (also needs agent live authorization)"),
                  ("DF_CLIENT_EMAIL_SENDING_ENABLED", s.client_email_sending_enabled, "Real report / lead-summary email to dealerships"),
                  ("DF_EMAIL_PROVIDER", s.email_provider, "mock = nothing is emailed")],
        agent=st, global_pause=controls.get_flag(db, controls.GLOBAL_AUTOMATION_PAUSE),
        connected=list(db.scalars(select(IntegrationConnection).where(IntegrationConnection.status == "connected"))),
        allowlist=list(db.scalars(select(PilotRecipient).order_by(PilotRecipient.kind, PilotRecipient.value))), kinds=pilot.KINDS,
        uncertain_msgs=list(db.scalars(select(CustomerMessage).where(CustomerMessage.delivery_status == "UNCERTAIN"))),
        uncertain_notes=list(db.scalars(select(DealerNotification).where(DealerNotification.status == "UNCERTAIN"))),
        unknown_outreach=list(db.scalars(select(OutreachMessage).where(OutreachMessage.status == "failed",
                                                                       OutreachMessage.error.like("Outcome unknown%")))),
        attempts=list(db.scalars(select(SendAttempt).order_by(SendAttempt.id.desc()).limit(100))),
        attempt_counts=dict(db.execute(select(SendAttempt.status, func.count()).group_by(SendAttempt.status)).all()),
        failed_jobs=list(db.scalars(select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(20))),
        health=agent.health(db), integrity=audit_mod.verify_integrity(db),
    )
    return render(request, "pilot.html", user, **ctx)


@router.post("/emergency-stop")
def emergency_stop(request: Request, reason: str = Form(""), user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    agent.set_status(db, "emergency_stopped", actor=user, reason=f"pilot console: {reason or 'no reason given'}")
    return _go(request, "EMERGENCY STOP ON: no provider call of any kind will be made. Lift it from the Agent page.", "warn")


@router.post("/global-pause")
def global_pause(request: Request, on: int = Form(...), reason: str = Form(""), user: User = Depends(require("admin")),
                 db: Session = Depends(get_db)):
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, bool(on), user_id=user.id, reason=reason or None)
    return _go(request, "All automated customer replies paused." if on else "Global customer-automation pause lifted.")


@router.post("/return-to-sandbox")
def return_to_sandbox(request: Request, reason: str = Form(""), user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    """Withdraw every live authorization recorded in the app. Env switches still need turning off by hand."""
    n = db.execute(update(IntegrationConnection).where(IntegrationConnection.kind == channels.CONNECTION_KIND,
                                                       IntegrationConnection.status == "connected")
                   .values(status="sandbox").execution_options(synchronize_session=False)).rowcount or 0
    st = agent.state(db)
    was_live = st.live_authorized
    agent.authorize_live(db, actor=user, authorize=False)
    audit_mod.event(db, "pilot.return_to_sandbox", actor_id=user.id, connections=n, outreach_live_revoked=was_live, reason=reason)
    return _go(request, f"Returned to sandbox: {n} customer-messaging connection(s) set to sandbox"
                        f"{' and outreach live authorization revoked' if was_live else ''}. Also set the DF_*_ENABLED switches to false and "
                        "restart web and worker.", "warn")


@router.post("/allowlist")
def add_recipient(request: Request, kind: str = Form(...), value: str = Form(...), label: str = Form(...),
                  user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    if len(label.strip()) < 3:
        return _go(request, "Say who this internal test recipient is (label, 3+ characters).", "error")
    try:
        pilot.add(db, kind, value, label=label.strip(), actor_id=user.id)
    except ValueError as exc:
        return _go(request, str(exc), "error")
    return _go(request, "Internal test recipient added.")


@router.post("/allowlist/{rid}/remove")
def remove_recipient(request: Request, rid: int, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    row = db.get(PilotRecipient, rid)
    if row is None:
        raise HTTPException(404, "Not found")
    pilot.remove(db, row, actor_id=user.id)
    return _go(request, "Removed.")


@router.post("/reconcile/{kind}/{ref_id}")
def reconcile(request: Request, kind: str, ref_id: int, delivered: int = Form(...), user: User = Depends(require("admin")),
              db: Session = Depends(get_db)):
    if kind == "message":
        msg = db.get(CustomerMessage, ref_id)
        if msg is None:
            raise HTTPException(404, "Not found")
        try:
            reconcile_uncertain(db, msg, delivered=bool(delivered), user_id=user.id)
        except ValueError as exc:
            return _go(request, str(exc), "error")
    elif kind == "notification":
        n = db.get(DealerNotification, ref_id)
        if n is None or n.status != "UNCERTAIN":
            raise HTTPException(404, "Not found")
        n.status = "SENT" if delivered else "PENDING"
        if not delivered:
            enqueue(db, "customer.notify_email", f"customer.notify_email:{n.id}:r{n.attempts}", {"notification_id": n.id}, max_attempts=4)
        audit_mod.event(db, "notification.reconciled", actor_id=user.id, client_id=n.client_id, entity_type="dealer_notification",
                        entity_id=n.id, delivered=bool(delivered))
    else:
        raise HTTPException(404, "Not found")
    return _go(request, "Marked delivered." if delivered else "Re-queued (you confirmed it was not delivered).")
