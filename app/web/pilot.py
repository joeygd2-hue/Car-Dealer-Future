"""Pilot console (platform admins): one place to see and control everything that could leave the platform."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    Escalation,
    Job,
    LiveAuthorization,
    Message,
    Notification,
    OutreachMessage,
    PilotRecipient,
    ResponseDraft,
    SendAttempt,
)
from app.security.auth import AuthContext
from app.services import audit, controls, jobs, send_gate
from app.services.inbox.outbound import reconcile_uncertain
from app.services.outreach import agent
from app.web.deps import admin_get, admin_post, redirect, render
from app.worker import worker_health

router = APIRouter(prefix="/admin/pilot")
KINDS = ("EMAIL", "PHONE", "META_PSID", "CHAT_VISITOR")


@router.get("")
def console(request: Request, auth: AuthContext = Depends(admin_get), db: Session = Depends(get_db)):
    s = get_settings()
    ctx = dict(
        live_env=s.live_integrations_enabled, pilot_mode=s.pilot_mode, emergency=send_gate.emergency_stopped(db),
        global_pause=controls.get_flag(db, controls.GLOBAL_AUTOMATION_PAUSE),
        outreach_state=agent.get_state(db).state,
        live_auths=list(db.scalars(select(LiveAuthorization).where(LiveAuthorization.revoked_at.is_(None)))),
        allowlist=list(db.scalars(select(PilotRecipient).order_by(PilotRecipient.kind, PilotRecipient.value))),
        drafts=list(db.scalars(select(ResponseDraft).where(ResponseDraft.status == "PENDING_REVIEW")
                               .order_by(ResponseDraft.id.desc()).limit(50))),
        outreach_pending=db.scalar(select(func.count(OutreachMessage.id)).where(OutreachMessage.status == "PENDING_APPROVAL")) or 0,
        escalations=list(db.scalars(select(Escalation).where(Escalation.status != "RESOLVED").order_by(Escalation.id.desc()).limit(50))),
        dead=list(db.scalars(select(Job).where(Job.status == "DEAD").order_by(Job.id.desc()).limit(50))),
        uncertain_msgs=list(db.scalars(select(Message).where(Message.delivery_status == "UNCERTAIN"))),
        uncertain_outreach=list(db.scalars(select(OutreachMessage).where(OutreachMessage.status == "UNCERTAIN"))),
        uncertain_notes=list(db.scalars(select(Notification).where(Notification.status == "UNCERTAIN"))),
        attempts=list(db.scalars(select(SendAttempt).order_by(SendAttempt.id.desc()).limit(100))),
        attempt_counts=dict(db.execute(select(SendAttempt.status, func.count()).group_by(SendAttempt.status)).all()),
        health=worker_health(db), integrity=audit.verify_integrity(db), kinds=KINDS,
    )
    db.commit()
    return render(request, "admin/pilot.html", db, **ctx)


@router.post("/emergency-stop")
def emergency(on: int = Form(...), reason: str = Form(""), auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    if not on and len(reason.strip()) < 5:
        return redirect("/admin/pilot", err="Give a reason (5+ characters) to lift the emergency stop.")
    controls.set_flag(db, send_gate.EMERGENCY_STOP_ALL, bool(on), user_id=auth.user.id, reason=reason or None)
    if on:  # also halt the outreach agent and cancel its queued work
        agent.emergency_stop(db, user_id=auth.user.id, reason=f"platform emergency stop: {reason or 'no reason given'}")
    db.commit()
    return redirect("/admin/pilot", msg="EMERGENCY STOP ON: nothing will leave the platform." if on
                    else "Platform emergency stop lifted. (The outreach agent stays stopped until you reset and start it.)")


@router.post("/return-to-sandbox")
def return_to_sandbox(reason: str = Form(""), auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    """Revoke every live authorization. Without an authorization every adapter resolves to sandbox."""
    revoked = 0
    for a in db.scalars(select(LiveAuthorization).where(LiveAuthorization.revoked_at.is_(None))):
        revoked += controls.revoke_live(db, a.scope, user_id=auth.user.id, dealership_id=a.dealership_id)
    audit.record(db, "pilot.return_to_sandbox", actor_type="USER", actor_user_id=auth.user.id, revoked=revoked, reason=reason)
    db.commit()
    return redirect("/admin/pilot", msg=f"Returned to sandbox: revoked {revoked} live authorization(s). "
                                        "Also set LIVE_INTEGRATIONS_ENABLED=false and restart web + worker.")


@router.post("/allowlist")
def add_recipient(kind: str = Form(...), value: str = Form(...), label: str = Form(...), auth: AuthContext = Depends(admin_post),
                  db: Session = Depends(get_db)):
    if kind not in KINDS or not value.strip() or len(label.strip()) < 3:
        return redirect("/admin/pilot", err="Kind, value and a label (who this internal test recipient is) are required.")
    norm = send_gate.normalize_recipient(kind, value)
    if not db.scalar(select(PilotRecipient.id).where(PilotRecipient.kind == kind, PilotRecipient.value == norm)):
        db.add(PilotRecipient(kind=kind, value=norm, label=label.strip(), added_by_id=auth.user.id))
        audit.record(db, "pilot.allowlist_added", actor_type="USER", actor_user_id=auth.user.id, kind=kind, value=norm,
                     label=label.strip())
    db.commit()
    return redirect("/admin/pilot", msg="Internal test recipient added.")


@router.post("/allowlist/{rid}/remove")
def remove_recipient(rid: int, auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    r = db.get(PilotRecipient, rid)
    if r:
        audit.record(db, "pilot.allowlist_removed", actor_type="USER", actor_user_id=auth.user.id, kind=r.kind, value=r.value)
        db.delete(r)
    db.commit()
    return redirect("/admin/pilot", msg="Removed.")


@router.post("/reconcile/{kind}/{ref_id}")
def reconcile(kind: str, ref_id: int, delivered: int = Form(...), auth: AuthContext = Depends(admin_post),
              db: Session = Depends(get_db)):
    try:
        if kind == "message":
            msg = db.get(Message, ref_id)
            if msg is None:
                raise HTTPException(404, "Not found")
            reconcile_uncertain(db, msg, delivered=bool(delivered), user_id=auth.user.id)
        elif kind == "outreach":
            om = db.get(OutreachMessage, ref_id)
            if om is None:
                raise HTTPException(404, "Not found")
            agent.reconcile_uncertain(db, om, delivered=bool(delivered), user_id=auth.user.id)
        elif kind == "notification":
            n = db.get(Notification, ref_id)
            if n is None or n.status != "UNCERTAIN":
                raise HTTPException(404, "Not found")
            n.status = "SENT" if delivered else "PENDING"
            if not delivered:
                jobs.enqueue(db, "notification.send_email", {"notification_id": n.id},
                             idempotency_key=f"notify-email:{n.id}:r{n.attempts}")
            audit.record(db, "notification.reconciled", actor_type="USER", actor_user_id=auth.user.id,
                         entity_type="notification", entity_id=n.id, delivered=bool(delivered))
        else:
            raise HTTPException(404, "Not found")
    except ValueError as exc:
        return redirect("/admin/pilot", err=str(exc))
    db.commit()
    return redirect("/admin/pilot", msg="Marked delivered." if delivered else "Re-queued (you confirmed it was not delivered).")
