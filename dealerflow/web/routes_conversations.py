"""Phase 3 customer conversations in the dealership workspace: inbox, conversation review and replies, test-drive
appointments, response settings (business details, FAQs/templates, automation, notifications), plus a JSON API.

Every route is scoped by ``dealer_ctx`` (server-side role check + tenant-bound session); objects are fetched with
``_owned`` so another dealership's ids return 404. Agency admins also get /conversations/pilot (messaging safety).
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db, utcnow
from ..models import (
    FAQ,
    MODE_CONTROLLED,
    MODE_DRAFT_ONLY,
    MODES,
    Appointment,
    ApprovedTemplate,
    Conversation,
    CustomerContact,
    Escalation,
    EvaluationRun,
    IntegrationConnection,
    Message,
    ModeAuthorization,
    Notification,
    PilotRecipient,
    ResponseDraft,
    SendAttempt,
    User,
    Vehicle,
)
from ..services.conversations import appointments as appt_svc
from ..services.conversations import channels, escalation, evaluation, notifications, runtime
from ..services.conversations.automation_policy import (
    AUTO_RESPONSE_GATE,
    CONTROLLABLE_INTENTS,
    effective_mode,
    get_settings_row,
    valid_mode_authorization,
)
from ..services.conversations.engine import ENGINE_VERSION
from ..services.conversations.facts import vehicle_title
from ..services.conversations.outbound import (
    current_fact_snapshot,
    queue_outbound,
    reconcile_uncertain,
    record_manual_external,
)
from ..services.conversations.processing import ingest_inbound
from ..services.conversations.send_gate import normalize_recipient
from ..services.conversations.summaries import build_summary, deliver_lead_summary
from ..services.onboarding import automation_enabled
from ..tenancy import ClientContext
from .deps import check_csrf, flash, render, require
from .routes_dealers import dealer_ctx, go, page

router = APIRouter(dependencies=[Depends(check_csrf)])

INTENT_CHOICES = ["greeting", "availability", "price", "mileage", "features", "hours", "location", "thanks"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_HOURS_LABEL = "Each day: open and close time (24h, local), or closed"


def _owned(ctx: ClientContext, model, obj_id: int | None):
    obj = ctx.db.get(model, obj_id) if obj_id else None
    if obj is None or obj.client_id != ctx.client_id:
        raise HTTPException(404, "Not found")
    return obj


def _members(ctx: ClientContext) -> list[User]:
    return list(ctx.db.scalars(select(User).where(User.client_id == ctx.client_id, User.active.is_(True)).order_by(User.email)))


def _conv_url(ctx: ClientContext, conv_id: int) -> str:
    return f"/dealers/{ctx.client_id}/conversations/{conv_id}"


def _require_dealership_admin(ctx: ClientContext) -> None:
    """Authorizing automated replies is the dealership's decision, made by one of its own admins."""
    if ctx.user is None or ctx.user.client_id != ctx.client_id or ctx.user.client_role != "dealer_admin":
        raise HTTPException(403, "Only a dealership administrator (a user of this dealership) can authorize automated replies")


# --- inbox -----------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/inbox")
def inbox(request: Request, status: str = "", channel: str = "", mine: int = 0, ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Conversation)
    if status:
        stmt = stmt.where(Conversation.status == status)
    if channel:
        stmt = stmt.where(Conversation.channel == channel)
    if mine and ctx.user:
        stmt = stmt.where(Conversation.assigned_user_id == ctx.user.id)
    convs = list(ctx.db.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(200)))
    vehicles = {v.id: v for v in ctx.db.scalars(select(Vehicle))}
    counts = {
        "pending_drafts": ctx.db.scalar(select(func.count(ResponseDraft.id)).where(ResponseDraft.status == "PENDING_REVIEW")) or 0,
        "open_escalations": ctx.db.scalar(select(func.count(Escalation.id)).where(Escalation.status != "RESOLVED")) or 0,
        "pending_appointments": ctx.db.scalar(select(func.count(Appointment.id)).where(
            Appointment.status.in_(["PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED"]))) or 0,
    }
    settings = get_settings_row(ctx.db, ctx.client_id)
    return page(request, ctx, "dealer_inbox.html", convs=convs, vehicles=vehicles, vtitle=vehicle_title, counts=counts,
                channels=channels.CUSTOMER_CHANNELS, channel_labels=channels.CHANNEL_LABELS, f_status=status,
                f_channel=channel, mine=mine, mode=effective_mode(ctx.db, settings), settings=settings,
                emergency=runtime.emergency_stopped(ctx.db))


@router.get("/dealers/{client_id}/conversations/{conversation_id}")
def conversation_view(conversation_id: int, request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    return _conversation_page(request, ctx, conversation_id)


def _conversation_page(request: Request, ctx: ClientContext, conversation_id: int, send_warnings: list[str] | None = None,
                       unsent_body: str | None = None):
    conv = _owned(ctx, Conversation, conversation_id)
    drafts = list(ctx.db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id)
                                 .order_by(ResponseDraft.id.desc()).limit(10)))
    pending = next((x for x in drafts if x.status == "PENDING_REVIEW"), None)
    escs = list(ctx.db.scalars(select(Escalation).where(Escalation.conversation_id == conv.id).order_by(Escalation.id.desc())))
    appts = list(ctx.db.scalars(select(Appointment).where(Appointment.conversation_id == conv.id).order_by(Appointment.id)))
    vehicle = ctx.db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    contact = ctx.db.get(CustomerContact, conv.contact_id) if conv.contact_id else None
    tz = ctx.client.timezone
    return page(request, ctx, "dealer_conversation.html", conv=conv, drafts=drafts, pending=pending, escalations=escs,
                appointments=appts, vehicle=vehicle, vtitle=vehicle_title, contact=contact, members=_members(ctx),
                sendable=conv.channel in channels.SENDABLE_CHANNELS, channel_labels=channels.CHANNEL_LABELS,
                vehicles=list(ctx.db.scalars(select(Vehicle).where(Vehicle.inventory_status != "removed").order_by(Vehicle.make))),
                local=lambda dt: appt_svc.utc_to_local(dt, tz) if dt else None,
                send_warnings=send_warnings or [], unsent_body=unsent_body)


def staff_message_warnings(db: Session, conv: Conversation, body: str, draft: ResponseDraft | None) -> list[str]:
    """Run the same safety checker on the exact text staff are about to send."""
    from ..services.conversations.engine import T
    from ..services.conversations.safety import validate_output

    _, fs = current_fact_snapshot(db, conv)
    allowed = fs.allowed_strings() + [x[conv.preferred_language] for x in T.values()]
    if draft:
        allowed.append(draft.body)
    # Staff may legitimately write in another language or confirm a time they booked themselves.
    return [v for v in validate_output(body, allowed_sources=allowed, language=conv.preferred_language,
                                       appointment_confirmed=True) if not v.startswith("language mismatch")]


def send_staff_reply(ctx: ClientContext, conv: Conversation, body: str, *, draft: ResponseDraft | None,
                     manual_external: bool, acknowledge_warnings: bool) -> dict:
    """Shared by the page and the API. Returns {"warnings": [...]} without sending when unacknowledged warnings exist."""
    body = body.strip()
    if not body:
        raise HTTPException(400, "Message is empty")
    if conv.opted_out:
        raise HTTPException(409, "The customer opted out. Messages cannot be sent from the platform.")
    warnings = staff_message_warnings(ctx.db, conv, body, draft)
    if warnings and not acknowledge_warnings:
        return {"queued": False, "warnings": warnings}
    if warnings:
        runtime.record(ctx.db, "message.staff_override", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                       entity_type="conversation", entity_id=conv.id, warnings=warnings)
    if draft:
        draft.reviewed_by_id, draft.reviewed_at = ctx.actor_id, utcnow()
        if draft.body != body:
            runtime.record(ctx.db, "draft.edited", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                           entity_type="response_draft", entity_id=draft.id)
    if conv.channel not in channels.SENDABLE_CHANNELS or manual_external:
        msg = record_manual_external(ctx.db, conv, body, user_id=ctx.actor_id, draft=draft)
        build_summary(ctx.db, conv)
        return {"queued": False, "manual_external": True, "message_id": msg.id, "warnings": warnings}
    msg = queue_outbound(ctx.db, conv, body, sender_type="STAFF", user_id=ctx.actor_id, draft=draft)
    if draft:
        draft.status = "SENT"
    if conv.assigned_user_id is None and ctx.user is not None and ctx.user.client_id == ctx.client_id:
        conv.assigned_user_id = ctx.user.id
    build_summary(ctx.db, conv)
    return {"queued": True, "message_id": msg.id, "warnings": warnings}


@router.post("/dealers/{client_id}/conversations/{conversation_id}/send")
def send_reply(conversation_id: int, request: Request, body: str = Form(...), draft_id: str = Form(""),
               manual_external: str = Form(""), acknowledge_warnings: str = Form(""),
               ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    draft = _owned(ctx, ResponseDraft, int(draft_id)) if draft_id.strip().isdigit() else None
    if draft and draft.conversation_id != conv.id:
        raise HTTPException(404, "Not found")
    try:
        out = send_staff_reply(ctx, conv, body, draft=draft, manual_external=manual_external == "on",
                               acknowledge_warnings=acknowledge_warnings == "on")
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        flash(request, exc.detail, "error")
        return go(_conv_url(ctx, conv.id))
    if out["warnings"] and not out.get("message_id"):
        return _conversation_page(request, ctx, conv.id, send_warnings=out["warnings"], unsent_body=body)
    flash(request, "Recorded as sent outside the platform. Paste the reply into the original channel."
          if out.get("manual_external") else "Reply queued. Its delivery status (SIMULATED unless live-authorized) appears next to it.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/conversations/{conversation_id}/drafts/{draft_id}/reject")
def reject_draft(conversation_id: int, draft_id: int, request: Request, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    draft = _owned(ctx, ResponseDraft, draft_id)
    if draft.conversation_id != conversation_id:
        raise HTTPException(404, "Not found")
    draft.status, draft.reviewed_by_id, draft.reviewed_at = "REJECTED", ctx.actor_id, utcnow()
    runtime.record(ctx.db, "draft.rejected", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                   entity_type="response_draft", entity_id=draft.id)
    flash(request, "Draft rejected.")
    return go(_conv_url(ctx, conversation_id))


@router.post("/dealers/{client_id}/conversations/{conversation_id}/assign")
def assign(conversation_id: int, request: Request, user_id: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    uid = int(user_id) if user_id.strip().isdigit() else None
    if uid is not None and uid not in {u.id for u in _members(ctx)}:
        raise HTTPException(400, "User is not a member of this dealership")  # prevents cross-tenant assignment
    conv.assigned_user_id = uid
    runtime.record(ctx.db, "conversation.assigned", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                   entity_type="conversation", entity_id=conv.id, assignee=uid)
    if uid:
        notifications.notify(ctx.db, ctx.client_id, kind="NEW_INQUIRY", subject=f"Conversation #{conv.id} assigned to you",
                             body="A customer conversation was assigned to you.", link=_conv_url(ctx, conv.id),
                             assigned_user_id=uid, dedupe_key=f"assign:{conv.id}:{uid}:{utcnow():%Y%m%d%H%M}")
    flash(request, "Assignment updated.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/conversations/{conversation_id}/takeover")
def takeover(conversation_id: int, request: Request, on: int = Form(...), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    conv.human_takeover = bool(on)
    conv.takeover_by_id = ctx.actor_id if on else None
    conv.takeover_at = utcnow() if on else None
    if not on:
        conv.human_requested = False
    runtime.record(ctx.db, "conversation.takeover_on" if on else "conversation.takeover_off", actor_type="USER",
                   actor_user_id=ctx.actor_id, client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id)
    flash(request, "Human takeover on: automation will not reply." if on else "Automation re-enabled for this conversation.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/conversations/{conversation_id}/vehicle")
def link_vehicle(conversation_id: int, request: Request, vehicle_id: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    vid = int(vehicle_id) if vehicle_id.strip().isdigit() else None
    if vid:
        _owned(ctx, Vehicle, vid)
    conv.vehicle_id = vid
    build_summary(ctx.db, conv)
    flash(request, "Vehicle link updated.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/conversations/{conversation_id}/close")
def close_conv(conversation_id: int, request: Request, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    conv.status = "CLOSED"
    runtime.record(ctx.db, "conversation.closed", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                   entity_type="conversation", entity_id=conv.id)
    flash(request, "Conversation closed.")
    return go(f"/dealers/{ctx.client_id}/inbox")


@router.post("/dealers/{client_id}/conversations/{conversation_id}/summary")
def email_summary(conversation_id: int, request: Request, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    outcome = deliver_lead_summary(ctx.db, conv)
    flash(request, {"sent": "Summary emailed to the dealership contact.",
                    "simulated": "Summary recorded as SIMULATED email (client email is not live-authorized).",
                    "already_delivered": "The summary was already delivered."}.get(outcome, f"Summary not sent: {outcome}"),
          "info" if outcome in ("sent", "simulated", "already_delivered") else "error")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/escalations/{escalation_id}/resolve")
def resolve_escalation(escalation_id: int, request: Request, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    esc = _owned(ctx, Escalation, escalation_id)
    escalation.resolve(ctx.db, esc, ctx.actor_id)
    build_summary(ctx.db, ctx.db.get(Conversation, esc.conversation_id))
    flash(request, "Escalation resolved.")
    return go(_conv_url(ctx, esc.conversation_id))


@router.post("/dealers/{client_id}/messages/{message_id}/reconcile")
def reconcile(message_id: int, request: Request, delivered: str = Form(...), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    msg = _owned(ctx, Message, message_id)
    try:
        reconcile_uncertain(ctx.db, msg, delivered=delivered == "yes", user_id=ctx.actor_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    flash(request, "Marked delivered." if delivered == "yes" else "Re-queued for delivery (a new send attempt).")
    return go(_conv_url(ctx, msg.conversation_id))


def marketplace_thread(label: str) -> str:
    return "mp:" + hashlib.sha256(label.strip().lower().encode()).hexdigest()[:24]


def log_marketplace_message(ctx: ClientContext, *, thread_label: str, text: str, vehicle_id: int | None, buyer_name: str):
    """Human-assisted Marketplace workflow: staff paste a buyer message they read in Facebook themselves."""
    if vehicle_id:
        _owned(ctx, Vehicle, vehicle_id)
    if thread_label.startswith("__existing__"):
        thread = thread_label.removeprefix("__existing__")
        if not ctx.db.scalar(select(Conversation.id).where(Conversation.channel == channels.MARKETPLACE_ASSISTED,
                                                           Conversation.external_conversation_id == thread)):
            raise HTTPException(404, "Not found")
    else:
        if not thread_label.strip():
            raise HTTPException(400, "Thread label is required")
        thread = marketplace_thread(thread_label)
    if not text.strip():
        raise HTTPException(400, "Message text is required")
    pid = hashlib.sha256(f"{ctx.client_id}:{thread}:{text.strip()}".encode()).hexdigest()[:40]  # duplicate paste → same id
    res = ingest_inbound(ctx.db, dealership=ctx.client, channel=channels.MARKETPLACE_ASSISTED, external_conversation_id=thread,
                         text=text.strip(), provider_message_id=pid, contact_hints={"name": buyer_name.strip() or None},
                         vehicle_id=vehicle_id)
    conv = res.conversation
    if conv.assigned_user_id is None and ctx.user is not None and ctx.user.client_id == ctx.client_id:
        conv.assigned_user_id = ctx.user.id
    runtime.record(ctx.db, "marketplace.intake", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                   entity_type="conversation", entity_id=conv.id, duplicate=not res.created)
    return res


@router.post("/dealers/{client_id}/marketplace/intake")
def marketplace_intake(request: Request, thread_label: str = Form(...), text: str = Form(...), vehicle_id: str = Form(""),
                       buyer_name: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    res = log_marketplace_message(ctx, thread_label=thread_label, text=text,
                                  vehicle_id=int(vehicle_id) if vehicle_id.strip().isdigit() else None, buyer_name=buyer_name)
    flash(request, "Message logged. A draft reply will appear shortly; copy it into Marketplace yourself."
          if res.created else "This message was already logged.")
    return go(_conv_url(ctx, res.conversation.id))


# --- appointments ----------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/appointments")
def appointments(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    appts = list(ctx.db.scalars(select(Appointment).order_by(Appointment.requested_start.desc()).limit(200)))
    vehicles = {v.id: v for v in ctx.db.scalars(select(Vehicle))}
    tz = ctx.client.timezone
    return page(request, ctx, "dealer_appointments.html", appts=appts, vehicles=vehicles, vtitle=vehicle_title,
                local=lambda dt: appt_svc.utc_to_local(dt, tz) if dt else None,
                calendar_connected=channels.dealership_calendar(ctx.db, ctx.client_id) is not None)


def appointment_action_apply(ctx: ClientContext, appt: Appointment, action: str, new_start: str = "", note: str = "") -> str:
    if action == "confirm":
        appt_svc.confirm(ctx.db, appt, user_id=ctx.actor_id)
        return "Test drive confirmed. A confirmation message was queued to the customer (see its delivery status)."
    if action == "decline":
        appt_svc.decline(ctx.db, appt, user_id=ctx.actor_id, note=note or None)
        return "Request declined. Contact the customer to offer another time."
    if action == "cancel":
        appt_svc.cancel(ctx.db, appt, actor=f"user:{ctx.actor_id}", user_id=ctx.actor_id, reason=note or None)
        return "Appointment cancelled."
    if action == "reschedule":
        local = datetime.fromisoformat(new_start)
        appt_svc.reschedule(ctx.db, appt, appt_svc.local_to_utc(local, ctx.client.timezone), user_id=ctx.actor_id)
        return "Moved to the new time; confirm it once the customer agrees."
    if action in ("complete", "no-show"):
        appt.status = "COMPLETED" if action == "complete" else "NO_SHOW"
        runtime.record(ctx.db, f"appointment.{appt.status.lower()}", actor_type="USER", actor_user_id=ctx.actor_id,
                       client_id=ctx.client_id, entity_type="appointment", entity_id=appt.id)
        return "Marked completed." if action == "complete" else "Marked no-show."
    raise HTTPException(404, "Not found")


@router.post("/dealers/{client_id}/appointments/{appointment_id}/{action}")
def appointment_action(appointment_id: int, action: str, request: Request, new_start: str = Form(""), note: str = Form(""),
                       ctx: ClientContext = Depends(dealer_ctx("respond"))):
    appt = _owned(ctx, Appointment, appointment_id)
    url = f"/dealers/{ctx.client_id}/appointments"
    try:
        with ctx.db.begin_nested():
            msg = appointment_action_apply(ctx, appt, action, new_start, note)
    except (ValueError, channels.CalendarError) as exc:
        flash(request, f"Could not {action}: {exc}", "error")
        return go(url)
    flash(request, msg)
    return go(url)


# --- response settings -----------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/replies")
def reply_settings(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    c = ctx.client
    s = get_settings_row(ctx.db, c.id)
    hours = {h.weekday: h for h in c.hours}
    faqs = list(ctx.db.scalars(select(FAQ).order_by(FAQ.id.desc())))
    templates = list(ctx.db.scalars(select(ApprovedTemplate).order_by(ApprovedTemplate.intent, ApprovedTemplate.id.desc())))
    conns = list(ctx.db.scalars(select(IntegrationConnection).where(IntegrationConnection.client_id == c.id,
                                                                    IntegrationConnection.kind == runtime.CONNECTION_KIND)))
    latest_eval = ctx.db.scalar(select(EvaluationRun).where(EvaluationRun.client_id == c.id, EvaluationRun.engine_version == ENGINE_VERSION)
                                .order_by(EvaluationRun.id.desc()))
    auths = list(ctx.db.scalars(select(ModeAuthorization).order_by(ModeAuthorization.id.desc())))
    prefs = notifications.get_prefs(ctx.db, c.id)
    sent = list(ctx.db.scalars(select(Notification).where(Notification.client_id == c.id).order_by(Notification.id.desc()).limit(30)))
    return page(request, ctx, "dealer_replies.html", s=s, hours=hours, days=DAYS, faqs=faqs, templates=templates,
                intents=INTENT_CHOICES, conns=conns, modes=MODES, mode=effective_mode(ctx.db, s), latest_eval=latest_eval,
                auths=auths, controllable=sorted(CONTROLLABLE_INTENTS),
                valid_b=valid_mode_authorization(ctx.db, c.id, "B_FAQ_AUTOMATION"),
                valid_c=valid_mode_authorization(ctx.db, c.id, MODE_CONTROLLED),
                gate_on=automation_enabled(ctx.db, c.id, AUTO_RESPONSE_GATE), gate=AUTO_RESPONSE_GATE,
                prefs=prefs, sent=sent, members=_members(ctx), channel_labels=channels.CHANNEL_LABELS,
                is_dealership_admin=bool(ctx.user and ctx.user.client_id == c.id and ctx.user.client_role == "dealer_admin"),
                chat_url=f"/chat/{c.public_chat_key}", emergency=runtime.emergency_stopped(ctx.db),
                global_pause=runtime.get_flag(ctx.db, runtime.GLOBAL_AUTOMATION_PAUSE))


@router.post("/dealers/{client_id}/replies/business")
async def save_business(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    form = await request.form()
    c = ctx.client
    for f in ("phone", "website", "address_line", "city", "state", "postal_code"):
        val = (str(form.get(f, "")).strip() or None)
        setattr(c, f, val[:2] if (f == "state" and val) else val)
    weekly = []
    for wd in range(7):
        closed = form.get(f"closed_{wd}") == "on"
        o, cl = str(form.get(f"open_{wd}") or ""), str(form.get(f"close_{wd}") or "")
        if not closed and (not o or not cl or o >= cl):
            flash(request, f"Invalid hours for {DAYS[wd]}.", "error")
            return go(f"/dealers/{c.id}/replies")
        weekly.append(None if closed else [o, cl])
    c.business_hours = {**(c.business_hours or {}), "weekly": weekly}
    approved = form.get("approve") == "on"
    c.business_info_approved = approved
    c.business_info_approved_at = utcnow() if approved else None
    c.business_info_approved_by_id = ctx.actor_id if approved else None
    runtime.record(ctx.db, "business_info.updated", actor_type="USER", actor_user_id=ctx.actor_id, client_id=c.id, approved=approved)
    flash(request, "Saved. " + ("Approved for use in customer replies." if approved else "Not approved: replies will not share it."))
    return go(f"/dealers/{c.id}/replies")


@router.post("/dealers/{client_id}/replies/faqs")
def save_faq(request: Request, question: str = Form(...), answer: str = Form(...), keywords: str = Form(""),
             language: str = Form("en"), auto_send_allowed: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    faq = FAQ(client_id=ctx.client_id, question=question.strip(), answer=answer.strip(),
              keywords=[k.strip() for k in keywords.split(",") if k.strip()], language="es" if language == "es" else "en",
              auto_send_allowed=auto_send_allowed == "on", approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(faq)
    ctx.db.flush()
    runtime.record(ctx.db, "faq.approved", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                   entity_type="faq", entity_id=faq.id, auto_send=faq.auto_send_allowed)
    flash(request, "FAQ approved.")
    return go(f"/dealers/{ctx.client_id}/replies#content")


@router.post("/dealers/{client_id}/replies/faqs/{faq_id}/deactivate")
def deactivate_faq(faq_id: int, request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    faq = _owned(ctx, FAQ, faq_id)
    faq.active = False
    runtime.record(ctx.db, "faq.deactivated", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                   entity_type="faq", entity_id=faq.id)
    flash(request, "FAQ deactivated.")
    return go(f"/dealers/{ctx.client_id}/replies#content")


@router.post("/dealers/{client_id}/replies/templates")
def save_template(request: Request, intent: str = Form(...), language: str = Form("en"), body: str = Form(...),
                  auto_send_allowed: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    if intent not in INTENT_CHOICES:
        raise HTTPException(400, "Unsupported intent")
    lang = "es" if language == "es" else "en"
    for old in ctx.db.scalars(select(ApprovedTemplate).where(ApprovedTemplate.intent == intent, ApprovedTemplate.language == lang)):
        old.active = False
    t = ApprovedTemplate(client_id=ctx.client_id, intent=intent, language=lang, body=body.strip(),
                         auto_send_allowed=auto_send_allowed == "on", approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(t)
    ctx.db.flush()
    runtime.record(ctx.db, "template.approved", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id,
                   entity_type="template", entity_id=t.id, intent=intent)
    flash(request, "Template approved.")
    return go(f"/dealers/{ctx.client_id}/replies#content")


@router.post("/dealers/{client_id}/replies/pause")
def pause_automation(request: Request, on: int = Form(...), reason: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    """Anyone who can answer customers may PAUSE immediately; resuming needs the manage permission."""
    if not on and not ctx.can("manage"):
        raise HTTPException(403, "Only a dealership administrator or agency manager can resume automation")
    s = get_settings_row(ctx.db, ctx.client_id)
    s.paused, s.paused_reason = bool(on), (reason or None) if on else None
    s.paused_by_id, s.paused_at = (ctx.actor_id, utcnow()) if on else (None, None)
    runtime.record(ctx.db, "automation.paused" if on else "automation.resumed", actor_type="USER", actor_user_id=ctx.actor_id,
                   client_id=ctx.client_id, reason=reason)
    flash(request, "Automation paused for this dealership." if on else "Automation resumed.")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


@router.post("/dealers/{client_id}/replies/channels/{connection_id}/pause")
def pause_channel(connection_id: int, request: Request, on: int = Form(...), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conn = ctx.db.get(IntegrationConnection, connection_id)
    if conn is None or conn.client_id != ctx.client_id or conn.kind != runtime.CONNECTION_KIND:
        raise HTTPException(404, "Not found")
    if not on and not ctx.can("manage"):
        raise HTTPException(403, "Only a dealership administrator or agency manager can resume a channel")
    conn.paused = bool(on)
    runtime.record(ctx.db, "channel.paused" if on else "channel.resumed", actor_type="USER", actor_user_id=ctx.actor_id,
                   client_id=ctx.client_id, entity_type="integration", entity_id=conn.id, provider=conn.provider)
    flash(request, "Channel updated.")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


@router.post("/dealers/{client_id}/replies/limits")
def save_limits(request: Request, max_conv: int = Form(...), max_dealer: int = Form(...), min_interval: int = Form(...),
                after_hours: str = Form(""), stale_hours: int = Form(...), threshold: float = Form(...),
                escalation_emails: str = Form(""), controlled_intents: list[str] = Form([]),
                ctx: ClientContext = Depends(dealer_ctx("manage"))):
    s = get_settings_row(ctx.db, ctx.client_id)
    s.max_auto_replies_per_conversation_per_hour = max(0, min(max_conv, 20))
    s.max_auto_replies_per_dealership_per_hour = max(0, min(max_dealer, 500))
    s.min_seconds_between_auto_replies = max(5, min_interval)
    s.auto_reply_outside_business_hours = after_hours == "on"
    s.stale_inventory_hours = max(1, stale_hours)
    s.confidence_threshold = max(0.5, min(threshold, 1.0))
    s.escalation_emails = [e.strip() for e in escalation_emails.split(",") if "@" in e]
    s.controlled_intents = [i for i in controlled_intents if i in CONTROLLABLE_INTENTS]
    runtime.record(ctx.db, "automation.limits_updated", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id)
    flash(request, "Rules saved.")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


@router.post("/dealers/{client_id}/replies/evaluate")
def run_eval(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    run = evaluation.run_and_record(ctx.db, user_id=ctx.actor_id, dealership_id=ctx.client_id)
    flash(request, f"Evaluation {'PASSED' if run.passed else 'FAILED'} ({run.total_cases} synthetic conversations).",
          "info" if run.passed else "error")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


def set_response_mode(ctx: ClientContext, mode: str, statement_ack: bool) -> None:
    if mode not in MODES:
        raise HTTPException(400, "Unknown mode")
    c = ctx.client
    s = get_settings_row(ctx.db, c.id)
    if mode != MODE_DRAFT_ONLY:
        _require_dealership_admin(ctx)
        run = ctx.db.scalar(select(EvaluationRun).where(EvaluationRun.client_id == c.id, EvaluationRun.engine_version == ENGINE_VERSION,
                                                        EvaluationRun.passed.is_(True)).order_by(EvaluationRun.id.desc()))
        if run is None or (utcnow() - run.created_at).days > 30:
            raise HTTPException(400, "Run a passing evaluation (within 30 days) before enabling this mode")
        if not statement_ack:
            raise HTTPException(400, "You must confirm the authorization statement")
        statement = (f"I, {ctx.user.name or ctx.user.email} ({ctx.user.email}), authorize {mode} for {c.name}. Automated replies "
                     "may only use verified inventory data, dealer-approved information and approved templates, must escalate "
                     "everything else, and can be paused at any time.")
        ctx.db.add(ModeAuthorization(client_id=c.id, mode=mode, statement=statement, authorized_by_id=ctx.actor_id,
                                     evaluation_run_id=run.id))
    for a in ctx.db.scalars(select(ModeAuthorization).where(ModeAuthorization.mode != mode, ModeAuthorization.revoked_at.is_(None))):
        a.revoked_at = utcnow()
    s.mode = mode
    runtime.record(ctx.db, "automation.mode_changed", actor_type="USER", actor_user_id=ctx.actor_id, client_id=c.id, mode=mode)


@router.post("/dealers/{client_id}/replies/mode")
def set_mode(request: Request, mode: str = Form(...), statement_ack: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    try:
        set_response_mode(ctx, mode, statement_ack == "on")
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        flash(request, exc.detail, "error")
        return go(f"/dealers/{ctx.client_id}/replies#automation")
    flash(request, f"Mode set to {mode}.")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


@router.post("/dealers/{client_id}/replies/notifications")
def save_notifications(request: Request, immediate_alerts: str = Form(""), daily_digest: str = Form(""), digest_hour: int = Form(8),
                       notify_assigned: str = Form(""), email_enabled: str = Form(""),
                       ctx: ClientContext = Depends(dealer_ctx("manage"))):
    prefs = notifications.get_prefs(ctx.db, ctx.client_id)
    prefs.immediate_alerts = immediate_alerts == "on"
    prefs.daily_digest = daily_digest == "on"
    prefs.digest_hour_local = max(0, min(23, digest_hour))
    prefs.notify_assigned_user = notify_assigned == "on"
    prefs.email_enabled = email_enabled == "on"
    runtime.record(ctx.db, "notifications.preferences_updated", actor_type="USER", actor_user_id=ctx.actor_id, client_id=ctx.client_id)
    flash(request, "Notification preferences saved.")
    return go(f"/dealers/{ctx.client_id}/replies#notifications")


@router.get("/dealers/{client_id}/notifications")
def my_notifications(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    items = []
    if ctx.user is not None:
        items = list(ctx.db.scalars(select(Notification).where(Notification.user_id == ctx.user.id, Notification.client_id == ctx.client_id,
                                                               Notification.channel == "IN_APP").order_by(Notification.id.desc()).limit(100)))
        for n in items:
            n.read_at = n.read_at or utcnow()
    return page(request, ctx, "dealer_notifications.html", items=items)


# --- JSON API --------------------------------------------------------------------------------------------


def _conv_json(ctx: ClientContext, c: Conversation, full: bool = False) -> dict:
    out = {"id": c.id, "channel": c.channel, "status": c.status, "language": c.preferred_language, "vehicle_id": c.vehicle_id,
           "assigned_user_id": c.assigned_user_id, "escalation_status": c.escalation_status, "opted_out": c.opted_out,
           "human_takeover": c.human_takeover, "appointment_status": c.appointment_status, "simulated": c.simulated,
           "updated_at": c.updated_at.isoformat()}
    if full:
        out["summary"] = c.summary
        out["messages"] = [{"id": m.id, "direction": m.direction, "sender_type": m.sender_type, "body": m.body, "language": m.language,
                            "delivery_status": m.delivery_status, "delivery_error": m.delivery_error, "created_at": m.created_at.isoformat()}
                           for m in c.messages]
        out["drafts"] = [{"id": d.id, "status": d.status, "body": d.body, "language": d.language, "decision": d.decision,
                          "decision_reasons": d.decision_reasons, "escalation_reasons": d.escalation_reasons, "intents": d.intents,
                          "confidence": d.confidence}
                         for d in ctx.db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == c.id).order_by(ResponseDraft.id))]
        out["escalations"] = [{"id": e.id, "status": e.status, "reasons": e.reasons, "recommended_action": e.recommended_action}
                              for e in ctx.db.scalars(select(Escalation).where(Escalation.conversation_id == c.id).order_by(Escalation.id))]
        out["appointments"] = [{"id": a.id, "status": a.status, "requested_start": a.requested_start.isoformat() if a.requested_start else None,
                                "confirmed_start": a.confirmed_start.isoformat() if a.confirmed_start else None}
                               for a in ctx.db.scalars(select(Appointment).where(Appointment.conversation_id == c.id).order_by(Appointment.id))]
    return out


@router.get("/api/v1/dealers/{client_id}/conversations")
def api_conversations(status: str = "", ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Conversation)
    if status:
        stmt = stmt.where(Conversation.status == status)
    return [_conv_json(ctx, c) for c in ctx.db.scalars(stmt.order_by(Conversation.id.desc()).limit(200))]


@router.get("/api/v1/dealers/{client_id}/conversations/{conversation_id}")
def api_conversation(conversation_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    return _conv_json(ctx, _owned(ctx, Conversation, conversation_id), full=True)


class SendIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    draft_id: int | None = None
    manual_external: bool = False
    acknowledge_warnings: bool = False


@router.post("/api/v1/dealers/{client_id}/conversations/{conversation_id}/send")
def api_send(conversation_id: int, body: SendIn, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    draft = _owned(ctx, ResponseDraft, body.draft_id) if body.draft_id else None
    if draft and draft.conversation_id != conv.id:
        raise HTTPException(404, "Not found")
    return send_staff_reply(ctx, conv, body.body, draft=draft, manual_external=body.manual_external,
                            acknowledge_warnings=body.acknowledge_warnings)


@router.post("/api/v1/dealers/{client_id}/conversations/{conversation_id}/summary")
def api_summary(conversation_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _owned(ctx, Conversation, conversation_id)
    outcome = deliver_lead_summary(ctx.db, conv)
    return {"delivery": outcome, "summary_delivered": conv.summary_delivered_at is not None, "summary": conv.summary}


class AppointmentActionIn(BaseModel):
    action: str
    new_start: str = ""
    note: str = ""


@router.post("/api/v1/dealers/{client_id}/appointments/{appointment_id}")
def api_appointment(appointment_id: int, body: AppointmentActionIn, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    appt = _owned(ctx, Appointment, appointment_id)
    try:
        with ctx.db.begin_nested():
            msg = appointment_action_apply(ctx, appt, body.action, body.new_start, body.note)
    except (ValueError, channels.CalendarError) as exc:
        raise HTTPException(400, f"Could not {body.action}: {exc}") from exc
    return {"id": appt.id, "status": appt.status, "message": msg}


class MarketplaceIn(BaseModel):
    thread_label: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5000)
    vehicle_id: int | None = None
    buyer_name: str = Field(default="", max_length=100)


@router.post("/api/v1/dealers/{client_id}/marketplace/intake", status_code=201)
def api_marketplace(body: MarketplaceIn, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    res = log_marketplace_message(ctx, thread_label=body.thread_label, text=body.text, vehicle_id=body.vehicle_id,
                                  buyer_name=body.buyer_name)
    return {"conversation_id": res.conversation.id, "message_id": res.message.id, "created": res.created}


# --- agency: messaging safety (pilot console) --------------------------------------------------------------


@router.get("/conversations/pilot")
def pilot_console(request: Request, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    from ..config import get_settings

    attempts = list(db.scalars(select(SendAttempt).order_by(SendAttempt.id.desc()).limit(100)))
    uncertain = list(db.scalars(select(Message).where(Message.delivery_status == "UNCERTAIN").order_by(Message.id)))
    by_status = dict(db.execute(select(SendAttempt.status, func.count()).group_by(SendAttempt.status)).all())
    return render(request, "conversations_pilot.html", user, attempts=attempts, uncertain=uncertain, by_status=by_status,
                  recipients=list(db.scalars(select(PilotRecipient).order_by(PilotRecipient.kind, PilotRecipient.value))),
                  global_pause=runtime.get_flag(db, runtime.GLOBAL_AUTOMATION_PAUSE), emergency=runtime.emergency_stopped(db),
                  live_enabled=get_settings().customer_messaging_live_enabled,
                  pilot_mode=get_settings().customer_messaging_pilot_mode)


@router.post("/conversations/pilot/pause")
def pilot_pause(request: Request, on: int = Form(...), reason: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(require("admin"))):
    runtime.set_flag(db, runtime.GLOBAL_AUTOMATION_PAUSE, bool(on), user_id=user.id, reason=reason or None)
    db.commit()
    flash(request, "Automated customer replies are paused for every dealership." if on else "Global automation pause lifted.")
    return go("/conversations/pilot")


@router.post("/conversations/pilot/recipients")
def pilot_add_recipient(request: Request, kind: str = Form(...), value: str = Form(...), label: str = Form(...),
                        db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    if kind not in ("EMAIL", "PHONE", "META_PSID", "CHAT_VISITOR"):
        raise HTTPException(400, "Unknown recipient kind")
    norm = normalize_recipient(kind, value)
    if not norm:
        raise HTTPException(400, "Value is required")
    if not db.scalar(select(PilotRecipient.id).where(PilotRecipient.kind == kind, PilotRecipient.value == norm)):
        db.add(PilotRecipient(kind=kind, value=norm, label=label.strip()[:200] or norm, added_by_id=user.id))
        runtime.record(db, "pilot.recipient_added", actor_type="USER", actor_user_id=user.id, kind=kind, label=label)
    db.commit()
    flash(request, "Internal test recipient added.")
    return go("/conversations/pilot")


@router.post("/conversations/pilot/recipients/{recipient_id}/remove")
def pilot_remove_recipient(recipient_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    r = db.get(PilotRecipient, recipient_id)
    if r is None:
        raise HTTPException(404, "Not found")
    runtime.record(db, "pilot.recipient_removed", actor_type="USER", actor_user_id=user.id, kind=r.kind, label=r.label)
    db.delete(r)
    db.commit()
    flash(request, "Recipient removed.")
    return go("/conversations/pilot")


@router.get("/api/v1/conversations/pilot/ledger")
def api_ledger(db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    return [{"id": a.id, "kind": a.kind, "ref_id": a.ref_id, "client_id": a.client_id, "channel": a.channel, "live": a.live,
             "status": a.status, "reasons": a.reasons, "error": a.error, "created_at": a.created_at.isoformat()}
            for a in db.scalars(select(SendAttempt).order_by(SendAttempt.id.desc()).limit(500))]
