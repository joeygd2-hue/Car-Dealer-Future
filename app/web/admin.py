"""Central administrator workspace (platform admins only): clients, integrations, automation,
errors, usage, live authorizations, evaluations and the outreach agent operator dashboard."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.integrations import registry
from app.integrations.sandbox import add_simulated_inbound_email
from app.models import (
    AuditLog,
    AutomationSettings,
    Campaign,
    Conversation,
    CostEntry,
    Dealership,
    Enrollment,
    EvaluationRun,
    IntegrationConnection,
    Job,
    LiveAuthorization,
    Membership,
    Message,
    OutreachFAQ,
    OutreachMessage,
    OutreachReply,
    Prospect,
    ProspectFact,
    SalesMeeting,
    SandboxRecord,
    Suppression,
    User,
)
from app.security.auth import AuthContext
from app.security.crypto import CredentialEncryptionError, encrypt_json
from app.security.passwords import hash_password
from app.services import audit, controls, evaluation, jobs
from app.services.automation_policy import effective_mode, get_settings_row
from app.services.outreach import agent
from app.services.outreach.compliance import add_suppression
from app.services.outreach.discovery import import_csv
from app.services.outreach.drafting import DEFAULT_STEPS
from app.services.outreach.metrics import dashboard as outreach_dashboard
from app.web.deps import admin_get, admin_post, redirect, render
from app.worker import worker_health

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(request: Request, auth: AuthContext = Depends(admin_get), db: Session = Depends(get_db)):
    dealerships = list(db.scalars(select(Dealership).order_by(Dealership.name)))
    rows = []
    for d in dealerships:
        s = get_settings_row(db, d.id)
        rows.append({
            "d": d, "mode": effective_mode(db, s), "configured_mode": s.mode, "paused": s.paused,
            "conversations": db.scalar(select(func.count(Conversation.id)).where(Conversation.dealership_id == d.id)) or 0,
            "messages_7d": db.scalar(select(func.count(Message.id)).where(
                Message.dealership_id == d.id, Message.created_at >= utcnow() - timedelta(days=7))) or 0,
            "failed": db.scalar(select(func.count(Message.id)).where(Message.dealership_id == d.id,
                                                                    Message.delivery_status == "FAILED")) or 0,
        })
    dead = list(db.scalars(select(Job).where(Job.status == "DEAD").order_by(Job.id.desc()).limit(25)))
    db.commit()
    return render(request, "admin/home.html", db, rows=rows, dead=dead, health=worker_health(db),
                  integrations=registry.integration_status(db),
                  global_pause=controls.get_flag(db, controls.GLOBAL_AUTOMATION_PAUSE),
                  live_auths=list(db.scalars(select(LiveAuthorization).order_by(LiveAuthorization.id.desc()).limit(30))),
                  scopes=registry.ALL_PLATFORM_SCOPES,
                  evals=list(db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(5))),
                  cost_total=db.scalar(select(func.coalesce(func.sum(CostEntry.amount_cents), 0.0))) or 0.0,
                  users=list(db.scalars(select(User).order_by(User.email))))


@router.post("/global-pause")
def global_pause(on: int = Form(...), reason: str = Form(""), auth: AuthContext = Depends(admin_post),
                 db: Session = Depends(get_db)):
    controls.set_flag(db, controls.GLOBAL_AUTOMATION_PAUSE, bool(on), user_id=auth.user.id, reason=reason or None)
    db.commit()
    return redirect("/admin", msg="GLOBAL automation pause is ON." if on else "Global automation pause lifted.")


@router.post("/dealerships")
def create_dealership(name: str = Form(...), timezone: str = Form("America/Chicago"), auth: AuthContext = Depends(admin_post),
                      db: Session = Depends(get_db)):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or secrets.token_hex(4)
    if db.scalar(select(Dealership.id).where(Dealership.slug == slug)):
        slug = f"{slug}-{secrets.token_hex(2)}"
    d = Dealership(name=name.strip(), slug=slug, timezone=timezone)
    db.add(d)
    db.flush()
    db.add(AutomationSettings(dealership_id=d.id))
    db.add(IntegrationConnection(dealership_id=d.id, provider=registry.WEBSITE_CHAT, external_account_id=f"web:{d.id}",
                                 display_name="Website chat", status="CONNECTED"))
    db.add(IntegrationConnection(dealership_id=d.id, provider=registry.MARKETPLACE_ASSISTED,
                                 external_account_id=f"mp:{d.id}", display_name="Marketplace (human-assisted)",
                                 status="CONNECTED"))
    audit.record(db, "dealership.created", actor_type="USER", actor_user_id=auth.user.id, dealership_id=d.id)
    db.commit()
    return redirect(f"/admin/dealerships/{d.id}", msg="Client created in draft-only mode.")


@router.get("/dealerships/{dealership_id}")
def dealership_detail(dealership_id: int, request: Request, auth: AuthContext = Depends(admin_get),
                      db: Session = Depends(get_db)):
    d = db.get(Dealership, dealership_id)
    if d is None:
        raise HTTPException(404, "Not found")
    members = list(db.scalars(select(Membership).where(Membership.dealership_id == d.id)))
    conns = list(db.scalars(select(IntegrationConnection).where(IntegrationConnection.dealership_id == d.id)))
    return render(request, "admin/dealership.html", db, d=d, members=members, conns=conns,
                  providers=["META_MESSENGER", "TWILIO_SMS", "ADF_LEAD", "GOOGLE_CALENDAR"],
                  settings_row=get_settings_row(db, d.id))


@router.post("/dealerships/{dealership_id}/status")
def dealership_status(dealership_id: int, status: str = Form(...), plan: str = Form(""),
                      auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    d = db.get(Dealership, dealership_id)
    if d is None or status not in ("ONBOARDING", "ACTIVE", "SUSPENDED"):
        raise HTTPException(404, "Not found")
    d.status, d.service_plan = status, plan or d.service_plan
    audit.record(db, "dealership.status", actor_type="USER", actor_user_id=auth.user.id, dealership_id=d.id, status=status)
    db.commit()
    return redirect(f"/admin/dealerships/{d.id}", msg="Updated.")


def _one_time(request: Request, db: Session, title: str, label: str, secret: str, back: str, note: str = ""):
    """Show a secret exactly once in the response body — never in a URL, log line or flash message."""
    return render(request, "admin/one_time.html", db, title=title, label=label, secret=secret, back=back, note=note)


@router.post("/dealerships/{dealership_id}/members")
def add_member(dealership_id: int, request: Request, email: str = Form(...), name: str = Form(...),
               role: str = Form("DEALER_STAFF"), auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    from app.web.auth_routes import issue_reset_token

    d = db.get(Dealership, dealership_id)
    if d is None or role not in ("DEALER_ADMIN", "DEALER_STAFF"):
        raise HTTPException(400, "Invalid request")
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    link = None
    if user is None:
        # No password is ever chosen or shown by the admin: the user sets one through a single-use link.
        user = User(email=email.strip().lower(), name=name.strip(), password_hash=hash_password(secrets.token_urlsafe(32)))
        db.add(user)
        db.flush()
        token = issue_reset_token(db, user, created_by_id=auth.user.id, purpose="SETUP")
        link = f"{get_settings().base_url.rstrip('/')}/password/reset/{token}"
    if not db.scalar(select(Membership.id).where(Membership.user_id == user.id, Membership.dealership_id == d.id)):
        db.add(Membership(user_id=user.id, dealership_id=d.id, role=role, receives_escalations=role == "DEALER_ADMIN"))
    audit.record(db, "membership.added", actor_type="USER", actor_user_id=auth.user.id, dealership_id=d.id,
                 member=user.email, role=role)
    db.commit()
    if link:
        return _one_time(request, db, f"Account created for {user.email}", "Account setup link (valid 72 hours, single use)",
                         link, f"/admin/dealerships/{d.id}", "Send it to the person over a channel you trust.")
    return redirect(f"/admin/dealerships/{d.id}", msg=f"Added existing user {user.email}.")


@router.post("/users/{user_id}/reset-link")
def admin_reset_link(user_id: int, request: Request, auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    from app.web.auth_routes import issue_reset_token

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    token = issue_reset_token(db, user, created_by_id=auth.user.id)
    db.commit()
    return _one_time(request, db, f"Password reset for {user.email}",
                     f"Reset link (valid {get_settings().password_reset_ttl_minutes} minutes, single use)",
                     f"{get_settings().base_url.rstrip('/')}/password/reset/{token}", "/admin",
                     "Existing sessions end when the password is changed.")


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    from app.security.auth import invalidate_sessions

    user = db.get(User, user_id)
    if user is None or user.id == auth.user.id:
        raise HTTPException(400, "Invalid request")
    user.is_active = False
    n = invalidate_sessions(db, user.id)
    audit.record(db, "user.deactivated", actor_type="USER", actor_user_id=auth.user.id, entity_type="user",
                 entity_id=user.id, sessions_ended=n)
    db.commit()
    return redirect("/admin", msg=f"{user.email} deactivated and signed out.")


@router.post("/dealerships/{dealership_id}/connections")
def add_connection(dealership_id: int, request: Request, provider: str = Form(...), external_account_id: str = Form(""),
                   display_name: str = Form(""), credentials_json: str = Form(""),
                   auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    d = db.get(Dealership, dealership_id)
    if d is None:
        raise HTTPException(404, "Not found")
    url = f"/admin/dealerships/{d.id}"
    conn = IntegrationConnection(dealership_id=d.id, provider=provider, display_name=display_name or provider,
                                 external_account_id=external_account_id.strip() or f"{provider.lower()}:{d.id}",
                                 status="SANDBOX")
    token = None
    if provider == registry.ADF_LEAD:
        token = secrets.token_urlsafe(24)
        conn.config = {"token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        conn.status = "CONNECTED"
    if credentials_json.strip():
        try:
            conn.encrypted_credentials = encrypt_json(json.loads(credentials_json))
        except (json.JSONDecodeError, CredentialEncryptionError) as exc:
            return redirect(url, err=f"Credentials not saved: {exc}")
        conn.status = "PENDING_APPROVAL"
    if db.scalar(select(IntegrationConnection.id).where(IntegrationConnection.provider == provider,
                                                        IntegrationConnection.external_account_id == conn.external_account_id)):
        return redirect(url, err="That account is already connected (possibly to another client).")
    db.add(conn)
    db.flush()
    audit.record(db, "integration.added", actor_type="USER", actor_user_id=auth.user.id, dealership_id=d.id,
                 entity_type="integration", entity_id=conn.id, provider=provider)
    db.commit()
    if token:
        return _one_time(request, db, "ADF lead intake created", f"Bearer token for POST /api/leads/adf/{conn.id}", token, url,
                         "Configure it in the lead provider's HTTP delivery settings. It cannot be shown again.")
    return redirect(url, msg="Connection saved (credentials encrypted). Live use still requires a live authorization.")


@router.post("/connections/{connection_id}/sandbox-failure")
def set_failure_mode(connection_id: int, mode: str = Form(""), auth: AuthContext = Depends(admin_post),
                     db: Session = Depends(get_db)):
    conn = db.get(IntegrationConnection, connection_id)
    if conn is None:
        raise HTTPException(404, "Not found")
    conn.config = {**(conn.config or {}), "sandbox_failure_mode": mode or None}
    db.commit()
    return redirect(f"/admin/dealerships/{conn.dealership_id}", msg=f"Sandbox failure mode: {mode or 'off'}")


@router.post("/live-authorizations")
def grant_live(scope: str = Form(...), statement: str = Form(...), dealership_id: int | None = Form(None),
               auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    if len(statement.strip()) < 20:
        return redirect("/admin", err="Write an explicit authorization statement (20+ characters).")
    controls.authorize_live(db, scope.strip(), user_id=auth.user.id, statement=statement.strip(), dealership_id=dealership_id)
    db.commit()
    return redirect("/admin", msg=f"Live authorization recorded for {scope}. LIVE_INTEGRATIONS_ENABLED must also be true.")


@router.post("/live-authorizations/revoke")
def revoke_live(scope: str = Form(...), dealership_id: int | None = Form(None), auth: AuthContext = Depends(admin_post),
                db: Session = Depends(get_db)):
    n = controls.revoke_live(db, scope, user_id=auth.user.id, dealership_id=dealership_id)
    db.commit()
    return redirect("/admin", msg=f"Revoked {n} authorization(s) for {scope}.")


@router.post("/evaluations/run")
def admin_eval(auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    run = evaluation.run_and_record(db, user_id=auth.user.id)
    db.commit()
    return redirect("/admin", msg=f"Evaluation {'PASSED' if run.passed else 'FAILED'}: {run.metrics}")


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    jobs.retry_dead(db, job_id)
    audit.record(db, "job.retried", actor_type="USER", actor_user_id=auth.user.id, entity_type="job", entity_id=job_id)
    db.commit()
    return redirect("/admin", msg=f"Job {job_id} re-queued.")


@router.get("/audit")
def admin_audit(request: Request, auth: AuthContext = Depends(admin_get), db: Session = Depends(get_db)):
    logs = list(db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(500)))
    return render(request, "admin/audit.html", db, logs=logs)


# ------------------------------------------------------------------------------- outreach agent


@router.get("/outreach")
def outreach(request: Request, auth: AuthContext = Depends(admin_get), db: Session = Depends(get_db)):
    st = agent.get_state(db)
    _, live_email = registry.outreach_email_sender(db)
    _, live_inbox = registry.outreach_inbox(db)
    ctx = dict(
        st=st, metrics=outreach_dashboard(db), live_email=live_email, live_inbox=live_inbox,
        emergency=controls.get_flag(db, controls.OUTREACH_EMERGENCY_STOP),
        approvals=list(db.scalars(select(OutreachMessage).where(OutreachMessage.status == "PENDING_APPROVAL")
                                  .order_by(OutreachMessage.id).limit(50))),
        replies=list(db.scalars(select(OutreachReply).order_by(OutreachReply.id.desc()).limit(30))),
        prospects=list(db.scalars(select(Prospect).order_by(Prospect.fit_score.desc(), Prospect.id.desc()).limit(100))),
        campaigns=list(db.scalars(select(Campaign).order_by(Campaign.id))),
        recent=list(db.scalars(select(OutreachMessage).where(OutreachMessage.status.in_(["SENT", "SIMULATED", "BLOCKED", "FAILED"]))
                               .order_by(OutreachMessage.id.desc()).limit(30))),
        meetings=list(db.scalars(select(SalesMeeting).order_by(SalesMeeting.id.desc()).limit(20))),
        suppressions=list(db.scalars(select(Suppression).order_by(Suppression.id.desc()).limit(50))),
        faqs=list(db.scalars(select(OutreachFAQ).order_by(OutreachFAQ.id.desc()))),
        activity=list(db.scalars(select(AuditLog).where(AuditLog.action.startswith("outreach."))
                                 .order_by(AuditLog.id.desc()).limit(40))),
        enrollments=db.scalar(select(func.count(Enrollment.id))) or 0,
        sandbox_out=list(db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_OUT")
                                    .order_by(SandboxRecord.id.desc()).limit(10))),
        health=worker_health(db),
    )
    db.commit()
    return render(request, "admin/outreach.html", db, **ctx)


@router.post("/outreach/control/{action}")
def outreach_control(action: str, reason: str = Form(""), auth: AuthContext = Depends(admin_post),
                     db: Session = Depends(get_db)):
    try:
        if action == "start":
            agent.start(db, user_id=auth.user.id, reason=reason or "started by operator")
        elif action == "pause":
            agent.pause(db, user_id=auth.user.id, reason=reason or "paused by operator")
        elif action == "stop":
            agent.stop(db, user_id=auth.user.id, reason=reason or "stopped by operator")
        elif action == "emergency-stop":
            agent.emergency_stop(db, user_id=auth.user.id, reason=reason or "emergency stop by operator")
        elif action == "reset-emergency":
            if len(reason.strip()) < 5:
                return redirect("/admin/outreach", err="Give a reason to reset the emergency stop.")
            agent.reset_emergency(db, user_id=auth.user.id, reason=reason)
        else:
            raise HTTPException(404, "Not found")
    except ValueError as exc:
        return redirect("/admin/outreach", err=str(exc))
    db.commit()
    return redirect("/admin/outreach", msg=f"Agent: {agent.get_state(db).state}")


@router.post("/outreach/settings")
def outreach_settings(max_per_day: int = Form(...), max_per_hour: int = Form(...), budget_cents: int = Form(...),
                      start_hour: int = Form(...), end_hour: int = Form(...), target_queue: int = Form(...),
                      max_retries: int = Form(...), cities: str = Form(""), auto_respond: str = Form(""),
                      weekdays: list[int] = Form([]), auth: AuthContext = Depends(admin_post),
                      db: Session = Depends(get_db)):
    st = agent.get_state(db)
    st.max_sends_per_day, st.max_sends_per_hour = max(0, max_per_day), max(0, max_per_hour)
    st.daily_budget_cents = max(0, budget_cents)
    st.send_window_start_hour, st.send_window_end_hour = max(0, min(start_hour, 23)), max(1, min(end_hour, 24))
    st.target_queue_size, st.max_task_retries = max(0, target_queue), max(1, min(max_retries, 10))
    st.target_cities = [c.strip() for c in cities.split(",") if c.strip()]
    st.send_weekdays = [w for w in weekdays if 0 <= w <= 6]
    st.auto_respond_routine = auto_respond == "on"
    audit.record(db, "outreach.settings_updated", actor_type="USER", actor_user_id=auth.user.id)
    db.commit()
    return redirect("/admin/outreach", msg="Operating limits saved.")


@router.post("/outreach/campaigns")
def create_campaign(name: str = Form(...), auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    c = Campaign(name=name.strip(), steps=DEFAULT_STEPS)
    db.add(c)
    db.flush()
    audit.record(db, "outreach.campaign_created", actor_type="USER", actor_user_id=auth.user.id, entity_type="campaign",
                 entity_id=c.id)
    db.commit()
    return redirect("/admin/outreach#campaigns", msg="Campaign created (DRAFT). Review its content, then approve and activate.")


@router.post("/outreach/campaigns/{campaign_id}/{action}")
def campaign_action(campaign_id: int, action: str, auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Not found")
    if action == "approve-content":
        c.content_approved_by_id, c.content_approved_at = auth.user.id, utcnow()
    elif action == "activate":
        if not c.content_approved_at:
            return redirect("/admin/outreach#campaigns", err="Approve the campaign content first.")
        c.status = "ACTIVE"
    elif action == "pause":
        c.status = "PAUSED"
    elif action == "toggle-approval":
        c.require_message_approval = not c.require_message_approval
    else:
        raise HTTPException(404, "Not found")
    audit.record(db, f"outreach.campaign_{action}", actor_type="USER", actor_user_id=auth.user.id, entity_type="campaign",
                 entity_id=c.id)
    db.commit()
    return redirect("/admin/outreach#campaigns", msg=f"Campaign updated ({action}).")


@router.post("/outreach/messages/{message_id}/{action}")
def message_action(message_id: int, action: str, body: str = Form(None), subject: str = Form(None), reason: str = Form(""),
                   auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    msg = db.get(OutreachMessage, message_id)
    if msg is None:
        raise HTTPException(404, "Not found")
    try:
        if action == "approve":
            agent.approve_message(db, msg, user_id=auth.user.id, edited_body=body, edited_subject=subject)
        elif action == "reject":
            agent.reject_message(db, msg, user_id=auth.user.id, reason=reason or "not specified")
        else:
            raise HTTPException(404, "Not found")
    except ValueError as exc:
        return redirect("/admin/outreach#approvals", err=str(exc))
    db.commit()
    return redirect("/admin/outreach#approvals", msg=f"Message {action}d. Sending still passes compliance checks.")


@router.post("/outreach/prospects/import")
async def prospects_import(file: UploadFile, auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        return redirect("/admin/outreach#prospects", err="CSV too large (max 2 MB).")
    text = raw.decode("utf-8-sig", "replace")
    out = import_csv(db, text, user_id=auth.user.id)
    db.commit()
    return redirect("/admin/outreach#prospects", msg=f"Imported {out['created']}, duplicates {out['duplicates']}, invalid {out['invalid']} (rows need name + source_url).")


@router.post("/outreach/prospects/{prospect_id}/{action}")
def prospect_action(prospect_id: int, action: str, key: str = Form(""), value: str = Form(""), source_url: str = Form(""),
                    email: str = Form(""), auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    p = db.get(Prospect, prospect_id)
    if p is None:
        raise HTTPException(404, "Not found")
    if action == "verify-email":
        if email.strip():
            p.email = email.strip().lower()
        p.email_verified_by_human = True
        db.add(ProspectFact(prospect_id=p.id, key="email", value=p.email or "", source_name="operator verification",
                            source_url=source_url or None, confidence="VERIFIED"))
        if p.status == "NEEDS_REVIEW":
            p.status = "NEW"
    elif action == "observation":
        if not value.strip() or not source_url.strip():
            return redirect("/admin/outreach#prospects", err="Observations need text and a source URL.")
        db.add(ProspectFact(prospect_id=p.id, key=key or "observation", value=value.strip(), source_name="operator",
                            source_url=source_url.strip(), confidence="VERIFIED", is_observation=True))
    elif action == "dnc":
        p.status, p.status_reason = "DO_NOT_CONTACT", "marked by operator"
        add_suppression(db, "EMAIL", p.email, "MANUAL_DNC", f"operator {auth.user.email}")
        if p.domain:
            add_suppression(db, "DOMAIN", p.domain, "MANUAL_DNC", f"operator {auth.user.email}")
        agent._stop_enrollments(db, p, "do_not_contact")
    elif action == "requalify":
        p.status = "NEW"
    else:
        raise HTTPException(404, "Not found")
    audit.record(db, f"outreach.prospect_{action}", actor_type="USER", actor_user_id=auth.user.id, entity_type="prospect",
                 entity_id=p.id)
    db.commit()
    return redirect("/admin/outreach#prospects", msg="Prospect updated.")


@router.post("/outreach/suppressions")
def add_supp(kind: str = Form(...), value: str = Form(...), auth: AuthContext = Depends(admin_post),
             db: Session = Depends(get_db)):
    if kind not in ("EMAIL", "DOMAIN", "PHONE"):
        raise HTTPException(400, "bad kind")
    add_suppression(db, kind, value, "MANUAL_DNC", f"operator {auth.user.email}")
    audit.record(db, "outreach.suppression_added", actor_type="USER", actor_user_id=auth.user.id, kind=kind)
    db.commit()
    return redirect("/admin/outreach#suppression", msg="Added to do-not-contact list.")


@router.post("/outreach/faqs")
def add_outreach_faq(question: str = Form(...), answer: str = Form(...), keywords: str = Form(""),
                     auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    db.add(OutreachFAQ(question=question, answer=answer, keywords=[k.strip() for k in keywords.split(",") if k.strip()],
                       approved_by_id=auth.user.id, approved_at=utcnow()))
    audit.record(db, "outreach.faq_approved", actor_type="USER", actor_user_id=auth.user.id)
    db.commit()
    return redirect("/admin/outreach#faqs", msg="Approved company answer saved.")


@router.post("/outreach/simulate-reply")
def simulate_reply(prospect_id: int = Form(...), body: str = Form(...), subject: str = Form("Re: your note"),
                   auth: AuthContext = Depends(admin_post), db: Session = Depends(get_db)):
    _, live_inbox = registry.outreach_inbox(db)
    if live_inbox:
        return redirect("/admin/outreach", err="Simulation is disabled while the live inbox is authorized.")
    p = db.get(Prospect, prospect_id)
    if p is None or not p.email:
        return redirect("/admin/outreach", err="Prospect has no email.")
    last = db.scalar(select(OutreachMessage).where(OutreachMessage.prospect_id == p.id, OutreachMessage.rfc_message_id.is_not(None))
                     .order_by(OutreachMessage.id.desc()))
    add_simulated_inbound_email(db, from_addr=p.email, subject=subject, body=body,
                                in_reply_to=last.rfc_message_id if last else None)
    audit.record(db, "outreach.simulated_reply_injected", actor_type="USER", actor_user_id=auth.user.id,
                 entity_type="prospect", entity_id=p.id, outcome="SIMULATED")
    db.commit()
    return redirect("/admin/outreach#replies", msg="Simulated reply placed in the sandbox inbox; the agent will pick it up.")
