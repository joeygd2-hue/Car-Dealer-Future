"""Phase 3 pages: the dealership's customer inbox, conversations, appointments, reply settings and notifications
(inside the dealership workspace at ``/dealers/{client_id}/...``), plus the agency overview at ``/conversations``.

Every workspace route resolves the tenant through ``dealer_ctx`` (tenant-scoped session: another client's ids are
404) and checks the role server-side: ``view`` to read, ``respond`` to reply/assign/handle appointments, ``manage``
for settings. Switching a dealership above draft-only (Mode B/C) additionally requires one of that dealership's
own administrators: it is the dealership's authorization, never the agency's.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db, utcnow
from ..models import (
    MODE_CONTROLLED,
    MODE_DRAFT_ONLY,
    MODE_FAQ,
    MODES,
    Appointment,
    Client,
    Conversation,
    ConversationMessage,
    DealerFAQ,
    Escalation,
    EvaluationRun,
    IntegrationConnection,
    ModeAuthorization,
    Notification,
    ResponseDraft,
    ResponseTemplate,
    User,
    Vehicle,
)
from ..services.conversations import appointments as appt_svc
from ..services.conversations import escalation, evaluation, notifications
from ..services.conversations.automation_policy import (
    CONTROLLABLE_INTENTS,
    GLOBAL_AUTOMATION_PAUSE,
    effective_mode,
    get_settings_row,
    valid_mode_authorization,
)
from ..services.conversations.channels import (
    CHANNEL_LABELS,
    CUSTOMER_CHANNELS,
    MARKETPLACE_ASSISTED,
    SENDABLE_CHANNELS,
    WEBSITE_CHAT,
    CalendarError,
    dealership_calendar,
)
from ..services.conversations.common import assignable_users, get_flag, local_to_utc, record, set_flag, utc_to_local
from ..services.conversations.inbox import ingest_inbound
from ..services.conversations.outbound import queue_outbound, reconcile_uncertain, record_manual_external
from ..services.conversations.responder.engine import ENGINE_VERSION
from ..services.conversations.responder.facts import parse_hours, vehicle_title
from ..services.conversations.send_gate import emergency_stopped
from ..services.conversations.summaries import build_summary
from ..services.dealer_errors import BadRequest, NotFound
from ..integrations.errors import IntegrationError
from ..tenancy import ClientContext
from .deps import check_csrf, flash, render, require
from .routes_dealers import dealer_ctx, go, page

router = APIRouter(dependencies=[Depends(check_csrf)])

INTENT_CHOICES = ["greeting", "availability", "price", "mileage", "features", "hours", "location", "thanks"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
OPEN_STATES = ("OPEN", "AWAITING_STAFF", "ESCALATED")


def _get(ctx: ClientContext, model, obj_id: int | None):
    obj = ctx.db.get(model, obj_id) if obj_id else None
    if obj is None or obj.client_id != ctx.client_id:
        raise NotFound("Not found")
    return obj


def _conv_url(ctx: ClientContext, conv_id: int) -> str:
    return f"/dealers/{ctx.client_id}/conversations/{conv_id}"


def _is_dealer_admin(ctx: ClientContext) -> bool:
    return not ctx.is_staff and ctx.role == "dealer_admin"


# --- inbox ------------------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/inbox")
def inbox(request: Request, status: str = "", channel: str = "", mine: int = 0, ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Conversation)
    if status:
        stmt = stmt.where(Conversation.status == status)
    if channel:
        stmt = stmt.where(Conversation.channel == channel)
    if mine and ctx.user:
        stmt = stmt.where(Conversation.assigned_user_id == ctx.user.id)
    convs = ctx.db.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(200)).all()
    vehicles = {v.id: v for v in ctx.db.scalars(select(Vehicle).where(Vehicle.inventory_status != "removed"))}
    q = lambda m, *w: ctx.db.scalar(select(func.count(m.id)).where(*w)) or 0  # noqa: E731
    stats = {"open": q(Conversation, Conversation.status.in_(OPEN_STATES)),
             "drafts": q(ResponseDraft, ResponseDraft.status == "PENDING_REVIEW"),
             "escalations": q(Escalation, Escalation.status != "RESOLVED"),
             "appointments": q(Appointment, Appointment.status.in_(("PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED")))}
    settings = get_settings_row(ctx.db, ctx.client_id)
    return page(request, ctx, "dealer_inbox.html", convs=convs, vehicles=vehicles, stats=stats, channels=CUSTOMER_CHANNELS,
                channel_labels=CHANNEL_LABELS, f_status=status, f_channel=channel, mine=mine,
                mode=effective_mode(ctx.db, settings), settings=settings, vehicle_title=vehicle_title)


@router.post("/dealers/{client_id}/marketplace/intake")
def marketplace_intake(request: Request, thread_label: str = Form(...), text: str = Form(...), vehicle_id: str = Form(""),
                       buyer_name: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    """Human-assisted Marketplace workflow: staff paste a buyer message they read in Facebook themselves."""
    vid = int(vehicle_id) if vehicle_id.strip().isdigit() else None
    if vid:
        _get(ctx, Vehicle, vid)
    if not text.strip() or not thread_label.strip():
        raise BadRequest("Paste the buyer's message and a thread label")
    thread = "mp:" + hashlib.sha256(thread_label.strip().lower().encode()).hexdigest()[:24]
    pid = hashlib.sha256(f"{thread}:{text.strip()}".encode()).hexdigest()[:40]  # duplicate paste → same id
    res = ingest_inbound(ctx.db, client=ctx.client, channel=MARKETPLACE_ASSISTED, external_conversation_id=thread,
                         text=text.strip(), provider_message_id=pid, contact_hints={"name": buyer_name.strip() or None},
                         vehicle_id=vid)
    if res.conversation.assigned_user_id is None and ctx.user:
        res.conversation.assigned_user_id = ctx.user.id
    record(ctx.db, "marketplace.intake", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="conversation",
           entity_id=res.conversation.id, duplicate=not res.created)
    flash(request, "Message logged. A draft reply appears shortly; copy it into Marketplace yourself."
          if res.created else "This message was already logged.")
    return go(_conv_url(ctx, res.conversation.id))


@router.post("/dealers/{client_id}/simulate")
def simulate_inquiry(request: Request, text: str = Form(...), customer_name: str = Form(""), vehicle_id: str = Form(""),
                     ctx: ClientContext = Depends(dealer_ctx("respond"))):
    """Agency staff only: record a SIMULATED website-chat message to test replies. Never counted as a real inquiry."""
    if not ctx.is_staff:
        raise HTTPException(403, "Only agency staff can simulate customer messages")
    vid = int(vehicle_id) if vehicle_id.strip().isdigit() else None
    if vid:
        _get(ctx, Vehicle, vid)
    if not text.strip():
        raise BadRequest("Message text is required")
    visitor = f"sim:{secrets.token_hex(6)}"
    res = ingest_inbound(ctx.db, client=ctx.client, channel=WEBSITE_CHAT, external_conversation_id=visitor,
                         text=text.strip()[:2000], provider_message_id=f"{visitor}:1",
                         contact_hints={"name": customer_name.strip() or None}, vehicle_id=vid, simulated=True)
    flash(request, "SIMULATED message recorded. The draft reply appears after the worker processes it.")
    return go(_conv_url(ctx, res.conversation.id))


# --- conversation -----------------------------------------------------------------------------------------------


def _conversation_context(ctx: ClientContext, conv: Conversation) -> dict:
    db = ctx.db
    drafts = db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id)
                        .order_by(ResponseDraft.id.desc()).limit(10)).all()
    return dict(conv=conv, drafts=drafts, pending=next((x for x in drafts if x.status == "PENDING_REVIEW"), None),
                escalations=db.scalars(select(Escalation).where(Escalation.conversation_id == conv.id)
                                       .order_by(Escalation.id.desc())).all(),
                appointments=db.scalars(select(Appointment).where(Appointment.conversation_id == conv.id)).all(),
                vehicle=db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None,
                members=assignable_users(db, ctx.client_id), sendable=conv.channel in SENDABLE_CHANNELS,
                vehicles=db.scalars(select(Vehicle).where(Vehicle.inventory_status != "removed").order_by(Vehicle.id)).all(),
                users={u.id: u for u in assignable_users(db, ctx.client_id)}, channel_labels=CHANNEL_LABELS,
                vehicle_title=vehicle_title, tz=ctx.client.timezone, local=lambda dt: utc_to_local(dt, ctx.client.timezone) if dt else None)


@router.get("/dealers/{client_id}/conversations/{conv_id}")
def conversation_view(request: Request, conv_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    conv = _get(ctx, Conversation, conv_id)
    return page(request, ctx, "dealer_conversation.html", **_conversation_context(ctx, conv))


def staff_message_warnings(db: Session, conv: Conversation, body: str, draft: ResponseDraft | None) -> list[str]:
    """Run the same safety checker on the exact text staff are about to send."""
    from ..services.conversations.outbound import current_fact_snapshot
    from ..services.conversations.responder.engine import T
    from ..services.conversations.responder.safety import validate_output

    _, fs = current_fact_snapshot(db, conv)
    allowed = fs.allowed_strings() + [x[conv.preferred_language] for x in T.values()]
    if draft:
        allowed.append(draft.body)
    # Staff may legitimately write in another language or confirm a time they booked themselves.
    return [v for v in validate_output(body, allowed_sources=allowed, language=conv.preferred_language,
                                       appointment_confirmed=True) if not v.startswith("language mismatch")]


@router.post("/dealers/{client_id}/conversations/{conv_id}/send")
def send_reply(request: Request, conv_id: int, body: str = Form(...), draft_id: str = Form(""),
               manual_external: int = Form(0), acknowledge_warnings: str = Form(""),
               ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _get(ctx, Conversation, conv_id)
    url = _conv_url(ctx, conv.id)
    body = body.strip()
    if not body:
        flash(request, "Message is empty.", "error")
        return go(url)
    draft = _get(ctx, ResponseDraft, int(draft_id)) if draft_id.strip().isdigit() else None
    if draft and draft.conversation_id != conv.id:
        raise NotFound("Not found")
    if conv.opted_out:
        flash(request, "The customer opted out. Messages cannot be sent from the platform.", "error")
        return go(url)
    warnings = staff_message_warnings(ctx.db, conv, body, draft)
    if warnings and acknowledge_warnings != "on":
        return page(request, ctx, "dealer_conversation.html", send_warnings=warnings, unsent_body=body,
                    unsent_draft_id=draft.id if draft else None, **_conversation_context(ctx, conv))
    if warnings:
        record(ctx.db, "message.staff_override", client_id=ctx.client_id, actor_id=ctx.actor_id,
               entity_type="conversation", entity_id=conv.id, warnings=warnings)
    if draft:
        draft.reviewed_by_id, draft.reviewed_at = ctx.actor_id, utcnow()
        if draft.body != body:
            record(ctx.db, "draft.edited", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="response_draft",
                   entity_id=draft.id)
    if conv.channel not in SENDABLE_CHANNELS or manual_external:
        record_manual_external(ctx.db, conv, body, user_id=ctx.actor_id, draft=draft)
        build_summary(ctx.db, conv)
        flash(request, "Recorded as sent outside the platform. Paste the reply into the original channel.")
        return go(url)
    queue_outbound(ctx.db, conv, body, sender_type="STAFF", user_id=ctx.actor_id, draft=draft)
    if draft:
        draft.status = "SENT"
    if conv.assigned_user_id is None:
        conv.assigned_user_id = ctx.actor_id
    build_summary(ctx.db, conv)
    flash(request, "Reply queued. Delivery status appears next to the message (SIMULATED: nothing leaves the platform).")
    return go(url)


@router.post("/dealers/{client_id}/conversations/{conv_id}/drafts/{draft_id}/reject")
def reject_draft(request: Request, conv_id: int, draft_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    draft = _get(ctx, ResponseDraft, draft_id)
    if draft.conversation_id != conv_id:
        raise NotFound("Not found")
    draft.status, draft.reviewed_by_id, draft.reviewed_at = "REJECTED", ctx.actor_id, utcnow()
    record(ctx.db, "draft.rejected", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="response_draft",
           entity_id=draft.id)
    flash(request, "Draft rejected.")
    return go(_conv_url(ctx, conv_id))


@router.post("/dealers/{client_id}/conversations/{conv_id}/assign")
def assign(request: Request, conv_id: int, user_id: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _get(ctx, Conversation, conv_id)
    uid = int(user_id) if user_id.strip().isdigit() else None
    if uid is not None and uid not in {u.id for u in assignable_users(ctx.db, ctx.client_id)}:
        raise BadRequest("That person cannot be assigned to this dealership's conversations")  # no cross-tenant assignment
    conv.assigned_user_id = uid
    record(ctx.db, "conversation.assigned", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="conversation",
           entity_id=conv.id, assignee=uid)
    if uid:
        notifications.notify(ctx.db, ctx.client_id, kind="NEW_INQUIRY", subject=f"Conversation #{conv.id} assigned to you",
                             body="A customer conversation was assigned to you.", link=_conv_url(ctx, conv.id),
                             assigned_user_id=uid, dedupe_key=f"assign:{conv.id}:{uid}:{utcnow():%Y%m%d%H%M}")
    flash(request, "Assignment updated.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/conversations/{conv_id}/takeover")
def takeover(request: Request, conv_id: int, on: int = Form(...), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _get(ctx, Conversation, conv_id)
    conv.human_takeover = bool(on)
    conv.takeover_by_id = ctx.actor_id if on else None
    conv.takeover_at = utcnow() if on else None
    if not on:
        conv.human_requested = False
    record(ctx.db, "conversation.takeover_on" if on else "conversation.takeover_off", client_id=ctx.client_id,
           actor_id=ctx.actor_id, entity_type="conversation", entity_id=conv.id)
    flash(request, "Human takeover on: automation will not reply." if on else "Automation re-enabled for this conversation.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/conversations/{conv_id}/vehicle")
def link_vehicle(request: Request, conv_id: int, vehicle_id: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _get(ctx, Conversation, conv_id)
    vid = int(vehicle_id) if vehicle_id.strip().isdigit() else None
    if vid:
        _get(ctx, Vehicle, vid)
    conv.vehicle_id = vid
    build_summary(ctx.db, conv)
    flash(request, "Vehicle link updated.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/conversations/{conv_id}/close")
def close_conv(request: Request, conv_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _get(ctx, Conversation, conv_id)
    conv.status = "CLOSED"
    record(ctx.db, "conversation.closed", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="conversation",
           entity_id=conv.id)
    flash(request, "Conversation closed.")
    return go(f"/dealers/{ctx.client_id}/inbox")


@router.post("/dealers/{client_id}/conversations/{conv_id}/messages/{msg_id}/reconcile")
def reconcile(request: Request, conv_id: int, msg_id: int, delivered: int = Form(...),
              ctx: ClientContext = Depends(dealer_ctx("manage"))):
    """An operator checked the channel for an UNCERTAIN send: mark it delivered, or re-queue it."""
    msg = _get(ctx, ConversationMessage, msg_id)
    if msg.conversation_id != conv_id:
        raise NotFound("Not found")
    try:
        reconcile_uncertain(ctx.db, msg, delivered=bool(delivered), user_id=ctx.actor_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    flash(request, "Marked delivered." if delivered else "Re-queued for delivery.")
    return go(_conv_url(ctx, conv_id))


@router.post("/dealers/{client_id}/escalations/{esc_id}/resolve")
def resolve_escalation(request: Request, esc_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    esc = _get(ctx, Escalation, esc_id)
    escalation.resolve(ctx.db, esc, ctx.actor_id)
    build_summary(ctx.db, ctx.db.get(Conversation, esc.conversation_id))
    flash(request, "Escalation resolved.")
    return go(_conv_url(ctx, esc.conversation_id))


@router.post("/dealers/{client_id}/vehicles/{vehicle_id}/verify")
def verify_vehicle(request: Request, vehicle_id: int, conflict_note: str = Form(""), back: str = Form(""),
                   ctx: ClientContext = Depends(dealer_ctx("edit"))):
    """A person confirmed price/availability now (or recorded that sources disagree)."""
    v = _get(ctx, Vehicle, vehicle_id)
    v.data_conflict_note = conflict_note.strip()[:1000] or None
    if not v.data_conflict_note:
        v.verified_at = utcnow()
    record(ctx.db, "vehicle.verified" if not v.data_conflict_note else "vehicle.conflict_recorded", client_id=ctx.client_id,
           actor_id=ctx.actor_id, entity_type="vehicle", entity_id=v.id)
    flash(request, "Price and availability marked verified now." if not v.data_conflict_note
          else "Conflict recorded: replies will not quote this vehicle's price or availability until it is cleared.")
    return go(back if back.startswith(f"/dealers/{ctx.client_id}/") else f"/dealers/{ctx.client_id}/vehicles/{v.id}")


# --- appointments -----------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/appointments")
def appointments(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    appts = ctx.db.scalars(select(Appointment).order_by(Appointment.requested_start.desc()).limit(200)).all()
    vehicles = {v.id: v for v in ctx.db.scalars(select(Vehicle))}
    tz = ctx.client.timezone
    return page(request, ctx, "dealer_appointments.html", appts=appts, vehicles=vehicles, vehicle_title=vehicle_title,
                local=lambda dt: utc_to_local(dt, tz) if dt else None,
                calendar_connected=dealership_calendar(ctx.db, ctx.client_id) is not None)


@router.post("/dealers/{client_id}/appointments/{appt_id}/{action}")
def appointment_action(request: Request, appt_id: int, action: str, new_start: str = Form(""), note: str = Form(""),
                       ctx: ClientContext = Depends(dealer_ctx("respond"))):
    appt = _get(ctx, Appointment, appt_id)
    url = f"/dealers/{ctx.client_id}/appointments"
    try:
        if action == "confirm":
            appt_svc.confirm(ctx.db, appt, user_id=ctx.actor_id)
            msg = "Appointment confirmed. The confirmation is queued to the customer (SIMULATED delivery)."
        elif action == "decline":
            appt_svc.decline(ctx.db, appt, user_id=ctx.actor_id, note=note or None)
            msg = "Request declined. Contact the customer to offer another time."
        elif action == "cancel":
            appt_svc.cancel(ctx.db, appt, actor=f"user:{ctx.actor_id}", user_id=ctx.actor_id, reason=note or None)
            msg = "Appointment cancelled."
        elif action == "reschedule":
            local = datetime.fromisoformat(new_start)
            appt_svc.reschedule(ctx.db, appt, local_to_utc(local, ctx.client.timezone), user_id=ctx.actor_id)
            msg = "Moved to the new time; confirm it once the customer agrees."
        elif action in ("complete", "no-show"):
            appt.status = "COMPLETED" if action == "complete" else "NO_SHOW"
            record(ctx.db, f"appointment.{appt.status.lower()}", client_id=ctx.client_id, actor_id=ctx.actor_id,
                   entity_type="appointment", entity_id=appt.id)
            msg = "Marked completed." if action == "complete" else "Marked no-show."
        else:
            raise NotFound("Not found")
    except (ValueError, CalendarError, IntegrationError) as exc:
        ctx.db.rollback()
        flash(request, f"Could not {action}: {exc}", "error")
        return go(url)
    flash(request, msg)
    return go(url)


# --- reply settings ---------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/replies")
def reply_settings(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    db = ctx.db
    settings = get_settings_row(db, ctx.client_id)
    conns = db.scalars(select(IntegrationConnection).where(IntegrationConnection.client_id == ctx.client_id,
                                                           IntegrationConnection.kind.in_(("customer_messaging", "calendar")))).all()
    latest_eval = db.scalar(select(EvaluationRun).where(EvaluationRun.engine_version == ENGINE_VERSION,
                                                        (EvaluationRun.client_id == ctx.client_id) | EvaluationRun.client_id.is_(None))
                            .order_by(EvaluationRun.id.desc()))
    from ..config import get_settings

    return page(request, ctx, "dealer_replies.html", s=settings, mode=effective_mode(db, settings), modes=MODES,
                conns=conns, latest_eval=latest_eval, controllable=sorted(CONTROLLABLE_INTENTS),
                auths=db.scalars(select(ModeAuthorization).order_by(ModeAuthorization.id.desc()).limit(10)).all(),
                valid_b=valid_mode_authorization(db, ctx.client_id, MODE_FAQ),
                valid_c=valid_mode_authorization(db, ctx.client_id, MODE_CONTROLLED),
                faqs=db.scalars(select(DealerFAQ).order_by(DealerFAQ.id.desc())).all(),
                templates_=db.scalars(select(ResponseTemplate).order_by(ResponseTemplate.intent, ResponseTemplate.language)).all(),
                intents=INTENT_CHOICES, days=DAYS, hours=parse_hours(ctx.client.business_hours),
                prefs=notifications.get_prefs(db, ctx.client_id), members=assignable_users(db, ctx.client_id),
                is_dealer_admin=_is_dealer_admin(ctx), base_url=get_settings().base_url.rstrip("/"),
                channels=CUSTOMER_CHANNELS, channel_labels=CHANNEL_LABELS, global_pause=get_flag(db, GLOBAL_AUTOMATION_PAUSE),
                estop=emergency_stopped(db), new_token=request.session.pop("adf_token", None))


@router.post("/dealers/{client_id}/replies/business")
async def save_business(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    form = await request.form()
    c = ctx.client
    for f in ("phone", "address_line", "city", "state", "postal_code"):
        val = (str(form.get(f, "")).strip() or None)
        setattr(c, f, val[:255] if val else None)
    hours: dict[str, list[str] | None] = {}
    for wd in range(7):
        if form.get(f"closed_{wd}") == "on":
            hours[str(wd)] = None
            continue
        o, cl = str(form.get(f"open_{wd}") or ""), str(form.get(f"close_{wd}") or "")
        if not o or not cl or o >= cl:
            raise BadRequest(f"Invalid hours for {DAYS[wd]} (or tick closed)")
        hours[str(wd)] = [o, cl]
    c.business_hours = hours
    approved = form.get("approve") == "on"
    c.business_info_approved_at = utcnow() if approved else None
    c.business_info_approved_by_id = ctx.actor_id if approved else None
    record(ctx.db, "business_info.updated", client_id=ctx.client_id, actor_id=ctx.actor_id, approved=approved)
    flash(request, "Saved. " + ("Approved for use in customer replies." if approved
                                else "Not approved: replies will not share phone, address or hours."))
    return go(f"/dealers/{ctx.client_id}/replies#business")


@router.post("/dealers/{client_id}/replies/faqs")
def save_faq(request: Request, question: str = Form(...), answer: str = Form(...), keywords: str = Form(""),
             language: str = Form("en"), auto_send_allowed: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    if not question.strip() or not answer.strip():
        raise BadRequest("Question and answer are required")
    faq = DealerFAQ(client_id=ctx.client_id, question=question.strip(), answer=answer.strip(),
                    keywords=[k.strip() for k in keywords.split(",") if k.strip()], language="es" if language == "es" else "en",
                    auto_send_allowed=auto_send_allowed == "on", approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(faq)
    ctx.db.flush()
    record(ctx.db, "faq.approved", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="faq", entity_id=faq.id,
           auto_send=faq.auto_send_allowed)
    flash(request, "FAQ approved.")
    return go(f"/dealers/{ctx.client_id}/replies#content")


@router.post("/dealers/{client_id}/replies/faqs/{faq_id}/deactivate")
def deactivate_faq(request: Request, faq_id: int, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    faq = _get(ctx, DealerFAQ, faq_id)
    faq.active = False
    record(ctx.db, "faq.deactivated", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="faq", entity_id=faq.id)
    flash(request, "FAQ deactivated.")
    return go(f"/dealers/{ctx.client_id}/replies#content")


@router.post("/dealers/{client_id}/replies/templates")
def save_template(request: Request, intent: str = Form(...), language: str = Form("en"), body: str = Form(...),
                  auto_send_allowed: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    if intent not in INTENT_CHOICES or not body.strip():
        raise BadRequest("Pick a supported intent and write the template text")
    lang = "es" if language == "es" else "en"
    for old in ctx.db.scalars(select(ResponseTemplate).where(ResponseTemplate.intent == intent, ResponseTemplate.language == lang)):
        old.active = False
    t = ResponseTemplate(client_id=ctx.client_id, intent=intent, language=lang, body=body.strip(),
                         auto_send_allowed=auto_send_allowed == "on", approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(t)
    ctx.db.flush()
    record(ctx.db, "template.approved", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="response_template",
           entity_id=t.id, intent=intent)
    flash(request, "Template approved.")
    return go(f"/dealers/{ctx.client_id}/replies#content")


@router.post("/dealers/{client_id}/replies/automation/pause")
def pause_automation(request: Request, on: int = Form(...), reason: str = Form(""),
                     ctx: ClientContext = Depends(dealer_ctx("respond"))):
    """Anyone who can respond may PAUSE immediately; only managers/dealership admins may resume."""
    s = get_settings_row(ctx.db, ctx.client_id)
    if not on and not ctx.can("manage"):
        raise HTTPException(403, "Only a dealership administrator or agency manager can resume automation")
    s.paused, s.paused_reason = bool(on), ((reason or None) if on else None)
    s.paused_by_id, s.paused_at = (ctx.actor_id, utcnow()) if on else (None, None)
    record(ctx.db, "automation.paused" if on else "automation.resumed", client_id=ctx.client_id, actor_id=ctx.actor_id,
           reason=reason or None)
    flash(request, "Automation paused for this dealership." if on else "Automation resumed.")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


@router.post("/dealers/{client_id}/replies/automation/limits")
async def save_limits(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    form = await request.form()
    s = get_settings_row(ctx.db, ctx.client_id)
    try:
        s.max_auto_replies_per_conversation_per_hour = max(0, min(int(form.get("max_conv", 3)), 20))
        s.max_auto_replies_per_dealership_per_hour = max(0, min(int(form.get("max_dealer", 30)), 500))
        s.min_seconds_between_auto_replies = max(5, int(form.get("min_interval", 30)))
        s.stale_inventory_hours = max(1, int(form.get("stale_hours", 24)))
        s.confidence_threshold = max(0.5, min(float(form.get("threshold", 0.85)), 1.0))
    except (TypeError, ValueError) as exc:
        raise BadRequest("Limits must be numbers") from exc
    s.auto_reply_outside_business_hours = form.get("after_hours") == "on"
    s.escalation_emails = [e.strip() for e in str(form.get("escalation_emails", "")).split(",") if "@" in e][:10]
    s.controlled_intents = [i for i in form.getlist("controlled_intents") if i in CONTROLLABLE_INTENTS]
    record(ctx.db, "automation.limits_updated", client_id=ctx.client_id, actor_id=ctx.actor_id)
    flash(request, "Rules saved.")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


@router.post("/dealers/{client_id}/replies/automation/evaluate")
def run_eval(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    run = evaluation.run_and_record(ctx.db, user_id=ctx.actor_id, client_id=ctx.client_id)
    flash(request, f"Evaluation {'PASSED' if run.passed else 'FAILED'} ({run.total_cases} synthetic conversations).",
          "info" if run.passed else "error")
    return go(f"/dealers/{ctx.client_id}/replies#automation")


@router.post("/dealers/{client_id}/replies/automation/mode")
def set_mode(request: Request, mode: str = Form(...), statement_ack: str = Form(""),
             ctx: ClientContext = Depends(dealer_ctx("manage"))):
    url = f"/dealers/{ctx.client_id}/replies#automation"
    if mode not in MODES:
        raise BadRequest("Unknown mode")
    s = get_settings_row(ctx.db, ctx.client_id)
    if mode != MODE_DRAFT_ONLY:
        if not _is_dealer_admin(ctx):
            raise HTTPException(403, "Only one of this dealership's administrators can authorize automated replies")
        run = ctx.db.scalar(select(EvaluationRun).where(EvaluationRun.client_id == ctx.client_id,
                                                        EvaluationRun.engine_version == ENGINE_VERSION,
                                                        EvaluationRun.passed.is_(True)).order_by(EvaluationRun.id.desc()))
        if run is None or (utcnow() - run.created_at).days > 30:
            flash(request, "Run a passing evaluation (within 30 days) before enabling this mode.", "error")
            return go(url)
        if statement_ack != "on":
            flash(request, "You must confirm the authorization statement.", "error")
            return go(url)
        user = ctx.user
        statement = (f"I, {user.name} ({user.email}), authorize {mode} for {ctx.client.name}. Automated replies may only use "
                     "verified inventory data, dealer-approved information and approved templates, must escalate everything "
                     "else, and can be paused at any time.")
        ctx.db.add(ModeAuthorization(client_id=ctx.client_id, mode=mode, statement=statement, authorized_by_id=user.id,
                                     evaluation_run_id=run.id))
    for a in ctx.db.scalars(select(ModeAuthorization).where(ModeAuthorization.mode != mode, ModeAuthorization.revoked_at.is_(None))):
        a.revoked_at = utcnow()
    s.mode = mode
    record(ctx.db, "automation.mode_changed", client_id=ctx.client_id, actor_id=ctx.actor_id, mode=mode)
    flash(request, f"Mode set to {mode}.")
    return go(url)


@router.post("/dealers/{client_id}/replies/channels")
def add_channel(request: Request, channel: str = Form(...), external_account_id: str = Form(""),
                ctx: ClientContext = Depends(dealer_ctx("manage"))):
    """Record a customer channel for this dealership. All channels are sandbox-only in this build."""
    if channel not in CUSTOMER_CHANNELS and channel != "CALENDAR":
        raise BadRequest("Unknown channel")
    kind, provider = ("calendar", "sandbox_calendar") if channel == "CALENDAR" else ("customer_messaging", channel.lower())
    conn = ctx.db.scalar(select(IntegrationConnection).where(IntegrationConnection.kind == kind,
                                                             IntegrationConnection.provider == provider))
    if conn is None:
        conn = IntegrationConnection(client_id=ctx.client_id, kind=kind, provider=provider, status="sandbox",
                                     authorization_note="Sandbox channel added in the dealership workspace", config={})
        ctx.db.add(conn)
    conn.external_account_id = external_account_id.strip()[:200] or conn.external_account_id
    if channel == "ADF_LEAD":
        token = secrets.token_urlsafe(32)
        conn.config = {**(conn.config or {}), "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        request.session["adf_token"] = token  # shown once on the next page view, never stored in clear text
    ctx.db.flush()
    record(ctx.db, "channel.connected", client_id=ctx.client_id, actor_id=ctx.actor_id, entity_type="integration",
           entity_id=conn.id, provider=provider, status="sandbox")
    flash(request, f"{CHANNEL_LABELS.get(channel, 'Test-drive calendar')} added (sandbox).")
    return go(f"/dealers/{ctx.client_id}/replies#channels")


@router.post("/dealers/{client_id}/replies/channels/{conn_id}/pause")
def pause_channel(request: Request, conn_id: int, on: int = Form(...), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conn = _get(ctx, IntegrationConnection, conn_id)
    if not on and not ctx.can("manage"):
        raise HTTPException(403, "Only a dealership administrator or agency manager can resume a channel")
    conn.paused = bool(on)
    record(ctx.db, "channel.paused" if on else "channel.resumed", client_id=ctx.client_id, actor_id=ctx.actor_id,
           entity_type="integration", entity_id=conn.id, provider=conn.provider)
    flash(request, "Channel updated.")
    return go(f"/dealers/{ctx.client_id}/replies#channels")


@router.post("/dealers/{client_id}/replies/notifications")
async def save_notifications(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    form = await request.form()
    prefs = notifications.get_prefs(ctx.db, ctx.client_id)
    prefs.immediate_alerts = form.get("immediate_alerts") == "on"
    prefs.daily_digest = form.get("daily_digest") == "on"
    try:
        prefs.digest_hour_local = max(0, min(23, int(form.get("digest_hour", 8))))
    except ValueError as exc:
        raise BadRequest("Digest hour must be a number") from exc
    prefs.notify_assigned_user = form.get("notify_assigned") == "on"
    prefs.email_enabled = form.get("email_enabled") == "on"
    allowed = {u.id for u in assignable_users(ctx.db, ctx.client_id)}
    prefs.escalation_user_ids = [int(x) for x in form.getlist("escalation_user_ids") if str(x).isdigit() and int(x) in allowed]
    record(ctx.db, "notifications.preferences_updated", client_id=ctx.client_id, actor_id=ctx.actor_id)
    flash(request, "Notification preferences saved.")
    return go(f"/dealers/{ctx.client_id}/replies#notifications")


@router.get("/dealers/{client_id}/notifications")
def my_notifications(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    items = ctx.db.scalars(select(Notification).where(Notification.user_id == ctx.actor_id, Notification.channel == "IN_APP")
                           .order_by(Notification.id.desc()).limit(100)).all()
    for n in items:
        n.read_at = n.read_at or utcnow()
    sent = ctx.db.scalars(select(Notification).where(Notification.channel == "EMAIL").order_by(Notification.id.desc()).limit(30)).all()
    return page(request, ctx, "dealer_notifications.html", items=items, sent=sent)


# --- agency overview --------------------------------------------------------------------------------------------


@router.get("/conversations")
def agency_overview(request: Request, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    def per_client(model, *where):
        return dict(db.execute(select(model.client_id, func.count(model.id)).where(*where).group_by(model.client_id)).all())

    clients = db.scalars(select(Client).where(Client.status != "cancelled").order_by(Client.name)).all()
    stats = {"open": per_client(Conversation, Conversation.status.in_(OPEN_STATES)),
             "drafts": per_client(ResponseDraft, ResponseDraft.status == "PENDING_REVIEW"),
             "escalations": per_client(Escalation, Escalation.status != "RESOLVED"),
             "appointments": per_client(Appointment, Appointment.status.in_(("PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED"))),
             "uncertain": per_client(ConversationMessage, ConversationMessage.delivery_status == "UNCERTAIN")}
    uncertain = db.scalars(select(ConversationMessage).where(ConversationMessage.delivery_status == "UNCERTAIN")
                           .order_by(ConversationMessage.id.desc()).limit(50)).all()
    evals = db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(5)).all()
    return render(request, "conversations_overview.html", user, clients=clients, stats=stats, uncertain=uncertain,
                  evals=evals, global_pause=get_flag(db, GLOBAL_AUTOMATION_PAUSE), estop=emergency_stopped(db))


@router.post("/conversations/global-pause")
def global_pause(request: Request, on: int = Form(...), reason: str = Form(""), user: User = Depends(require("rep")),
                 db: Session = Depends(get_db)):
    """Any rep may pause automated customer replies for every dealership; resuming needs a manager."""
    from ..security import role_at_least

    if not on and not role_at_least(user.role, "manager"):
        raise HTTPException(403, "Requires manager role to resume")
    set_flag(db, GLOBAL_AUTOMATION_PAUSE, bool(on), actor_id=user.id, reason=reason or None)
    db.commit()
    flash(request, "Automated customer replies paused for every dealership." if on else "Global pause lifted.")
    return go("/conversations")


@router.post("/conversations/evaluate")
def agency_eval(request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    run = evaluation.run_and_record(db, user_id=user.id)
    db.commit()
    flash(request, f"Evaluation {'PASSED' if run.passed else 'FAILED'} ({run.total_cases} synthetic conversations).",
          "info" if run.passed else "error")
    return go("/conversations")

