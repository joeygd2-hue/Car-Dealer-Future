"""Phase 3 customer messaging inside the dealership workspace (/dealers/{client_id}/...).

Every route obtains a tenant-scoped ``ClientContext`` from ``dealer_ctx`` (404 for another dealership, server-side
permission check) and every object is fetched through ``_own`` so ids from another dealership return 404.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select

from .. import audit
from ..db import utcnow
from ..integrations import customer_channels as channels
from ..integrations.errors import IntegrationError
from ..models import User
from ..models_customer import (
    FAQ,
    MODE_CONTROLLED,
    MODE_DRAFT_ONLY,
    MODE_FAQ,
    MODES,
    ApprovedResponseTemplate,
    CustomerConversation,
    CustomerMessage,
    DealerNotification,
    Escalation,
    EvaluationRun,
    ModeAuthorization,
    ResponseDraft,
    TestDriveAppointment,
)
from ..models_dealer import Vehicle
from ..models_ops import IntegrationConnection
from ..services.customer import appointments as appt_svc
from ..services.customer import escalation, evaluation, notify
from ..services.customer.outbound import queue_outbound, reconcile_uncertain, record_manual_external
from ..services.customer.policy import (
    CONTROLLABLE_INTENTS,
    ONBOARDING_FEATURE,
    effective_mode,
    get_settings_row,
    valid_mode_authorization,
)
from ..services.customer.processing import ingest_inbound
from ..services.customer.summaries import build_summary
from ..services.onboarding import automation_enabled
from ..services.responder.engine import ENGINE_VERSION
from ..services.responder.facts import client_hours, vehicle_title
from ..tenancy import ClientContext
from .deps import check_csrf, flash
from .routes_dealers import dealer_ctx, go, page

router = APIRouter(dependencies=[Depends(check_csrf)])

INTENT_CHOICES = ["greeting", "availability", "price", "mileage", "features", "hours", "location", "thanks"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _own(ctx: ClientContext, model, oid: int | None):
    obj = ctx.db.get(model, oid) if oid else None
    if obj is None or getattr(obj, "client_id", None) != ctx.client_id:
        raise HTTPException(404, "Not found")
    return obj


def _users(ctx: ClientContext) -> list[User]:
    """People who may be assigned a conversation: this dealership's users and agency staff."""
    return list(ctx.db.scalars(select(User).where(User.active.is_(True), (User.client_id == ctx.client_id) | User.client_id.is_(None))
                                .order_by(User.email)))


def _conv_url(ctx: ClientContext, conv_id: int) -> str:
    return f"/dealers/{ctx.client_id}/inbox/{conv_id}"


# --- inbox ------------------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/inbox")
def inbox(request: Request, status: str = "", channel: str = "", mine: int = 0, ctx: ClientContext = Depends(dealer_ctx())):
    db = ctx.db
    stmt = select(CustomerConversation)
    if status:
        stmt = stmt.where(CustomerConversation.status == status)
    if channel:
        stmt = stmt.where(CustomerConversation.channel == channel)
    if mine and ctx.user:
        stmt = stmt.where(CustomerConversation.assigned_user_id == ctx.user.id)
    convs = list(db.scalars(stmt.order_by(CustomerConversation.updated_at.desc()).limit(200)))
    count = lambda m, *w: db.scalar(select(func.count(m.id)).where(*w)) or 0  # noqa: E731
    stats = {
        "pending_drafts": count(ResponseDraft, ResponseDraft.status == "PENDING_REVIEW"),
        "open_escalations": count(Escalation, Escalation.status != "RESOLVED"),
        "pending_appointments": count(TestDriveAppointment, TestDriveAppointment.status.in_(["PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED"])),
        "uncertain": count(CustomerMessage, CustomerMessage.delivery_status == "UNCERTAIN"),
    }
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.removed_at.is_(None)))}
    settings = get_settings_row(db, ctx.client_id)
    return page(request, ctx, "customer_inbox.html", convs=convs, vehicles=vehicles, vtitle=vehicle_title, stats=stats,
                channels=channels.CUSTOMER_CHANNELS, f_status=status, f_channel=channel, mine=mine,
                mode=effective_mode(db, settings), settings=settings)


@router.get("/dealers/{client_id}/inbox/{conversation_id}")
def conversation_view(request: Request, conversation_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    return _conversation_page(request, ctx, conversation_id)


def _conversation_page(request: Request, ctx: ClientContext, conversation_id: int, **extra):
    db = ctx.db
    conv = _own(ctx, CustomerConversation, conversation_id)
    drafts = list(db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id).order_by(ResponseDraft.id.desc()).limit(10)))
    escs = list(db.scalars(select(Escalation).where(Escalation.conversation_id == conv.id).order_by(Escalation.id.desc())))
    appts = list(db.scalars(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == conv.id)))
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    return page(request, ctx, "inbox_conversation.html", conv=conv, drafts=drafts,
                pending=next((x for x in drafts if x.status == "PENDING_REVIEW"), None), escalations=escs, appointments=appts,
                vehicle=vehicle, vtitle=vehicle_title, users=_users(ctx), sendable=conv.channel in channels.SENDABLE_CHANNELS,
                vehicles=list(db.scalars(select(Vehicle).where(Vehicle.removed_at.is_(None)).order_by(Vehicle.make, Vehicle.model))),
                **extra)


def staff_message_warnings(ctx: ClientContext, conv: CustomerConversation, body: str, draft: ResponseDraft | None) -> list[str]:
    """Run the same safety checker on the exact text staff are about to send."""
    from ..services.customer.outbound import current_fact_snapshot
    from ..services.responder.engine import T
    from ..services.responder.safety import validate_output

    _, fs = current_fact_snapshot(ctx.db, conv)
    allowed = fs.allowed_strings() + [x[conv.preferred_language] for x in T.values()]
    if draft:
        allowed.append(draft.body)
    # Staff may legitimately write in another language or confirm a time they booked themselves.
    return [v for v in validate_output(body, allowed_sources=allowed, language=conv.preferred_language, appointment_confirmed=True)
            if not v.startswith("language mismatch")]


@router.post("/dealers/{client_id}/inbox/{conversation_id}/send")
def send_reply(request: Request, conversation_id: int, body: str = Form(...), draft_id: int | None = Form(None),
               manual_external: int = Form(0), acknowledge_warnings: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    db = ctx.db
    conv = _own(ctx, CustomerConversation, conversation_id)
    url = _conv_url(ctx, conv.id)
    body = body.strip()
    if not body:
        flash(request, "Message is empty.", "error")
        return go(url)
    draft = _own(ctx, ResponseDraft, draft_id) if draft_id else None
    if draft and draft.conversation_id != conv.id:
        raise HTTPException(404, "Not found")
    if conv.opted_out:
        flash(request, "The customer opted out. Messages cannot be sent from the platform.", "error")
        return go(url)
    warnings = staff_message_warnings(ctx, conv, body, draft)
    if warnings and acknowledge_warnings != "on":
        return _conversation_page(request, ctx, conv.id, send_warnings=warnings, unsent_body=body, unsent_draft_id=draft_id)
    if warnings:
        audit.event(db, "message.staff_override", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="conversation",
                    entity_id=conv.id, warnings=warnings)
    if draft:
        draft.reviewed_by_id, draft.reviewed_at = ctx.actor_id, utcnow()
        if draft.body != body:
            audit.event(db, "draft.edited", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="response_draft", entity_id=draft.id)
    if conv.channel not in channels.SENDABLE_CHANNELS or manual_external:
        record_manual_external(db, conv, body, user_id=ctx.actor_id, draft=draft)
        build_summary(db, conv)
        flash(request, "Recorded as sent outside the platform. Paste the reply into the original channel.")
        return go(url)
    queue_outbound(db, conv, body, sender_type="STAFF", user_id=ctx.actor_id, draft=draft)
    if draft:
        draft.status = "SENT"
    conv.assigned_user_id = conv.assigned_user_id or ctx.actor_id
    build_summary(db, conv)
    flash(request, "Reply queued for delivery. Its delivery status appears next to the message.")
    return go(url)


@router.post("/dealers/{client_id}/inbox/{conversation_id}/drafts/{draft_id}/reject")
def reject_draft(request: Request, conversation_id: int, draft_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    draft = _own(ctx, ResponseDraft, draft_id)
    if draft.conversation_id != conversation_id:
        raise HTTPException(404, "Not found")
    draft.status, draft.reviewed_by_id, draft.reviewed_at = "REJECTED", ctx.actor_id, utcnow()
    audit.event(ctx.db, "draft.rejected", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="response_draft", entity_id=draft.id)
    flash(request, "Draft rejected.")
    return go(_conv_url(ctx, conversation_id))


@router.post("/dealers/{client_id}/inbox/{conversation_id}/assign")
def assign(request: Request, conversation_id: int, user_id: int | None = Form(None), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _own(ctx, CustomerConversation, conversation_id)
    if user_id is not None and user_id not in {u.id for u in _users(ctx)}:
        raise HTTPException(400, "That user cannot work on this dealership")  # prevents cross-tenant assignment
    conv.assigned_user_id = user_id
    audit.event(ctx.db, "conversation.assigned", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="conversation",
                entity_id=conv.id, assignee=user_id)
    if user_id:
        notify.notify(ctx.db, ctx.client_id, kind="NEW_INQUIRY", subject=f"Conversation #{conv.id} assigned to you",
                      body="A customer conversation was assigned to you.", link=_conv_url(ctx, conv.id), assigned_user_id=user_id,
                      dedupe_key=f"assign:{conv.id}:{user_id}:{utcnow():%Y%m%d%H%M}")
    flash(request, "Assignment updated.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/inbox/{conversation_id}/takeover")
def takeover(request: Request, conversation_id: int, on: int = Form(...), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _own(ctx, CustomerConversation, conversation_id)
    conv.human_takeover = bool(on)
    conv.takeover_by_id = ctx.actor_id if on else None
    conv.takeover_at = utcnow() if on else None
    if not on:
        conv.human_requested = False
    audit.event(ctx.db, "conversation.takeover_on" if on else "conversation.takeover_off", actor_id=ctx.actor_id,
                client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id)
    flash(request, "Human takeover on: automation will not reply." if on else "Automation re-enabled for this conversation.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/inbox/{conversation_id}/vehicle")
def link_vehicle(request: Request, conversation_id: int, vehicle_id: int | None = Form(None), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _own(ctx, CustomerConversation, conversation_id)
    if vehicle_id:
        _own(ctx, Vehicle, vehicle_id)
    conv.vehicle_id = vehicle_id
    build_summary(ctx.db, conv)
    flash(request, "Vehicle link updated.")
    return go(_conv_url(ctx, conv.id))


@router.post("/dealers/{client_id}/inbox/{conversation_id}/close")
def close_conv(request: Request, conversation_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conv = _own(ctx, CustomerConversation, conversation_id)
    conv.status = "CLOSED"
    audit.event(ctx.db, "conversation.closed", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id)
    flash(request, "Conversation closed.")
    return go(f"/dealers/{ctx.client_id}/inbox")


@router.post("/dealers/{client_id}/inbox/{conversation_id}/messages/{message_id}/reconcile")
def reconcile(request: Request, conversation_id: int, message_id: int, delivered: int = Form(...),
              ctx: ClientContext = Depends(dealer_ctx("manage"))):
    """A person checked the channel: mark an UNCERTAIN message delivered, or re-queue it (never automatic)."""
    msg = _own(ctx, CustomerMessage, message_id)
    if msg.conversation_id != conversation_id:
        raise HTTPException(404, "Not found")
    try:
        reconcile_uncertain(ctx.db, msg, delivered=bool(delivered), user_id=ctx.actor_id)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return go(_conv_url(ctx, conversation_id))
    flash(request, "Marked delivered." if delivered else "Re-queued for one more delivery attempt.")
    return go(_conv_url(ctx, conversation_id))


@router.post("/dealers/{client_id}/escalations/{escalation_id}/resolve")
def resolve_escalation(request: Request, escalation_id: int, ctx: ClientContext = Depends(dealer_ctx("respond"))):
    esc = _own(ctx, Escalation, escalation_id)
    escalation.resolve(ctx.db, esc, ctx.actor_id)
    build_summary(ctx.db, ctx.db.get(CustomerConversation, esc.conversation_id))
    flash(request, "Escalation resolved.")
    return go(_conv_url(ctx, esc.conversation_id))


@router.post("/dealers/{client_id}/inbox/marketplace")
def marketplace_intake(request: Request, thread_label: str = Form(...), text: str = Form(...), vehicle_id: int | None = Form(None),
                       buyer_name: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    """Human-assisted Marketplace workflow: staff paste a buyer message they read in Facebook themselves."""
    db = ctx.db
    if vehicle_id:
        _own(ctx, Vehicle, vehicle_id)
    if not text.strip():
        flash(request, "Paste the buyer's message.", "error")
        return go(f"/dealers/{ctx.client_id}/inbox")
    if thread_label.startswith("__existing__"):
        thread = thread_label.removeprefix("__existing__")
        if not db.scalar(select(CustomerConversation.id).where(CustomerConversation.channel == channels.MARKETPLACE_ASSISTED,
                                                               CustomerConversation.external_conversation_id == thread)):
            raise HTTPException(404, "Not found")
    else:
        thread = "mp:" + hashlib.sha256(f"{ctx.client_id}:{thread_label.strip().lower()}".encode()).hexdigest()[:24]
    pid = hashlib.sha256(f"{ctx.client_id}:{thread}:{text.strip()}".encode()).hexdigest()[:40]  # duplicate paste -> same id
    res = ingest_inbound(db, dealership=ctx.client, channel=channels.MARKETPLACE_ASSISTED, external_conversation_id=thread,
                         text=text.strip(), provider_message_id=pid, contact_hints={"name": buyer_name.strip() or None}, vehicle_id=vehicle_id)
    conv = res.conversation
    conv.assigned_user_id = conv.assigned_user_id or ctx.actor_id
    audit.event(db, "marketplace.intake", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id,
                duplicate=not res.created)
    flash(request, "Message logged. A draft reply appears once the worker processes it; copy it into Marketplace yourself."
          if res.created else "This message was already logged.")
    return go(_conv_url(ctx, conv.id))


# --- appointments ---------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/appointments")
def appointments(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    appts = list(ctx.db.scalars(select(TestDriveAppointment).order_by(TestDriveAppointment.requested_start.desc()).limit(200)))
    vehicles = {v.id: v for v in ctx.db.scalars(select(Vehicle))}
    tz = ctx.client.timezone
    return page(request, ctx, "inbox_appointments.html", appts=appts, vehicles=vehicles, vtitle=vehicle_title,
                local=lambda dt: appt_svc.utc_to_local(dt, tz) if dt else None,
                calendar_connected=appt_svc.dealership_calendar(ctx.db, ctx.client_id) is not None)


@router.post("/dealers/{client_id}/appointments/{appointment_id}/{action}")
def appointment_action(request: Request, appointment_id: int, action: str, new_start: str = Form(""), note: str = Form(""),
                       ctx: ClientContext = Depends(dealer_ctx("respond"))):
    db = ctx.db
    appt = _own(ctx, TestDriveAppointment, appointment_id)
    url = f"/dealers/{ctx.client_id}/appointments"
    with db.begin_nested() as sp:
        try:
            if action == "confirm":
                appt_svc.confirm(db, appt, user_id=ctx.actor_id)
                msg = "Appointment confirmed. The confirmation is queued to the customer (delivery status is in the conversation)."
            elif action == "decline":
                appt_svc.decline(db, appt, user_id=ctx.actor_id, note=note or None)
                msg = "Request declined. Contact the customer to offer another time."
            elif action == "cancel":
                appt_svc.cancel(db, appt, actor=f"user:{ctx.actor_id}", user_id=ctx.actor_id, reason=note or None)
                msg = "Appointment cancelled."
            elif action == "reschedule":
                local = datetime.fromisoformat(new_start)
                appt_svc.reschedule(db, appt, appt_svc.local_to_utc(local, ctx.client.timezone), user_id=ctx.actor_id)
                msg = "Moved to the new time; confirm it once the customer agrees."
            elif action == "complete":
                appt.status, msg = "COMPLETED", "Marked completed."
            elif action == "no-show":
                appt.status, msg = "NO_SHOW", "Marked no-show."
            else:
                raise HTTPException(404, "Not found")
        except (ValueError, IntegrationError) as exc:
            sp.rollback()
            flash(request, f"Could not {action}: {exc}", "error")
            return go(url)
    flash(request, msg)
    return go(url)


# --- response content: business info, FAQs, templates --------------------------------------------------------


@router.get("/dealers/{client_id}/responses")
def responses_page(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    db = ctx.db
    faqs = list(db.scalars(select(FAQ).order_by(FAQ.id.desc())))
    tpls = list(db.scalars(select(ApprovedResponseTemplate).order_by(ApprovedResponseTemplate.intent, ApprovedResponseTemplate.id.desc())))
    return page(request, ctx, "inbox_responses.html", faqs=faqs, templates_=tpls, intents=INTENT_CHOICES, days=DAYS,
                hours=client_hours(ctx.client), can_manage=ctx.can("manage"))


@router.post("/dealers/{client_id}/responses/business")
async def save_business(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    form = await request.form()
    c = ctx.client
    for f in ("phone", "address_line", "city", "state", "postal_code"):
        setattr(c, f, (str(form.get(f, "")).strip() or None))
    hours: dict[str, list[str] | None] = {}
    for wd in range(7):
        if form.get(f"closed_{wd}") == "on":
            hours[str(wd)] = None
            continue
        o, cl = str(form.get(f"open_{wd}") or ""), str(form.get(f"close_{wd}") or "")
        if not o or not cl or o >= cl:
            flash(request, f"Invalid hours for {DAYS[wd]}.", "error")
            return go(f"/dealers/{ctx.client_id}/responses")
        hours[str(wd)] = [o, cl]
    c.business_hours = hours
    approved = form.get("approve") == "on"
    c.business_info_approved = approved
    audit.event(ctx.db, "business_info.updated", actor_id=ctx.actor_id, client_id=ctx.client_id, approved=approved)
    flash(request, "Saved. " + ("Approved for use in customer replies." if approved else "Not approved: the assistant will not share it."))
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/faqs")
def save_faq(request: Request, question: str = Form(...), answer: str = Form(...), keywords: str = Form(""), language: str = Form("en"),
             auto_send_allowed: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    faq = FAQ(client_id=ctx.client_id, question=question.strip(), answer=answer.strip(),
              keywords=[k.strip() for k in keywords.split(",") if k.strip()], language="es" if language == "es" else "en",
              auto_send_allowed=auto_send_allowed == "on", approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(faq)
    ctx.db.flush()
    audit.event(ctx.db, "faq.approved", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="faq", entity_id=faq.id,
                auto_send=faq.auto_send_allowed)
    flash(request, "FAQ approved.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/faqs/{faq_id}/deactivate")
def deactivate_faq(request: Request, faq_id: int, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    faq = _own(ctx, FAQ, faq_id)
    faq.active = False
    audit.event(ctx.db, "faq.deactivated", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="faq", entity_id=faq.id)
    flash(request, "FAQ deactivated.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/templates")
def save_template(request: Request, intent: str = Form(...), language: str = Form("en"), body: str = Form(...),
                  auto_send_allowed: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    if intent not in INTENT_CHOICES:
        flash(request, "Unsupported intent.", "error")
        return go(f"/dealers/{ctx.client_id}/responses")
    lang = "es" if language == "es" else "en"
    for old in ctx.db.scalars(select(ApprovedResponseTemplate).where(ApprovedResponseTemplate.intent == intent,
                                                                     ApprovedResponseTemplate.language == lang)):
        old.active = False
    t = ApprovedResponseTemplate(client_id=ctx.client_id, intent=intent, language=lang, body=body.strip(),
                                 auto_send_allowed=auto_send_allowed == "on", approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(t)
    ctx.db.flush()
    audit.event(ctx.db, "template.approved", actor_id=ctx.actor_id, client_id=ctx.client_id, entity_type="response_template",
                entity_id=t.id, intent=intent)
    flash(request, "Template approved.")
    return go(f"/dealers/{ctx.client_id}/responses")


# --- automation -----------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/automation")
def automation_page(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    db = ctx.db
    s = get_settings_row(db, ctx.client_id)
    conns = list(db.scalars(select(IntegrationConnection).where(IntegrationConnection.client_id == ctx.client_id,
                                                                IntegrationConnection.kind == channels.CONNECTION_KIND)))
    latest_eval = db.scalar(select(EvaluationRun).where(EvaluationRun.engine_version == ENGINE_VERSION).order_by(EvaluationRun.id.desc()))
    auths = list(db.scalars(select(ModeAuthorization).order_by(ModeAuthorization.id.desc())))
    return page(request, ctx, "inbox_automation.html", s=s, conns=conns, modes=MODES, mode=effective_mode(db, s), latest_eval=latest_eval,
                auths=auths, controllable=sorted(CONTROLLABLE_INTENTS), valid_b=valid_mode_authorization(db, ctx.client_id, MODE_FAQ),
                valid_c=valid_mode_authorization(db, ctx.client_id, MODE_CONTROLLED),
                onboarding_ok=automation_enabled(db, ctx.client_id, ONBOARDING_FEATURE), onboarding_feature=ONBOARDING_FEATURE,
                can_authorize=ctx.can("authorize_automation"), can_manage=ctx.can("manage"), can_respond=ctx.can("respond"))


@router.post("/dealers/{client_id}/automation/pause")
def pause_automation(request: Request, on: int = Form(...), reason: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    """Anyone who can respond may PAUSE immediately; resuming needs the manage permission."""
    s = get_settings_row(ctx.db, ctx.client_id)
    if not on and not ctx.can("manage"):
        raise HTTPException(403, "Only a dealership admin or agency manager can resume automation")
    s.paused, s.paused_reason = bool(on), (reason or None) if on else None
    s.paused_by_id, s.paused_at = (ctx.actor_id, utcnow()) if on else (None, None)
    audit.event(ctx.db, "automation.paused" if on else "automation.resumed", actor_id=ctx.actor_id, client_id=ctx.client_id, reason=reason)
    flash(request, "Automation paused for this dealership." if on else "Automation resumed.")
    return go(f"/dealers/{ctx.client_id}/automation")


@router.post("/dealers/{client_id}/automation/channels/{connection_id}/pause")
def pause_channel(request: Request, connection_id: int, on: int = Form(...), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    conn = _own(ctx, IntegrationConnection, connection_id)
    if not on and not ctx.can("manage"):
        raise HTTPException(403, "Only a dealership admin or agency manager can resume a channel")
    conn.paused = bool(on)
    audit.event(ctx.db, "channel.paused" if on else "channel.resumed", actor_id=ctx.actor_id, client_id=ctx.client_id,
                entity_type="integration_connection", entity_id=conn.id, provider=conn.provider)
    flash(request, "Channel updated.")
    return go(f"/dealers/{ctx.client_id}/automation")


@router.post("/dealers/{client_id}/automation/limits")
def save_limits(request: Request, max_conv: int = Form(...), max_dealer: int = Form(...), min_interval: int = Form(...),
                after_hours: str = Form(""), stale_hours: int = Form(...), threshold: float = Form(...), escalation_emails: str = Form(""),
                controlled_intents: list[str] = Form([]), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    s = get_settings_row(ctx.db, ctx.client_id)
    s.max_auto_replies_per_conversation_per_hour = max(0, min(max_conv, 20))
    s.max_auto_replies_per_dealership_per_hour = max(0, min(max_dealer, 500))
    s.min_seconds_between_auto_replies = max(5, min_interval)
    s.auto_reply_outside_business_hours = after_hours == "on"
    s.stale_inventory_hours = max(1, stale_hours)
    s.confidence_threshold = max(0.5, min(threshold, 1.0))
    s.escalation_emails = [e.strip() for e in escalation_emails.split(",") if "@" in e]
    s.controlled_intents = [i for i in controlled_intents if i in CONTROLLABLE_INTENTS]
    audit.event(ctx.db, "automation.limits_updated", actor_id=ctx.actor_id, client_id=ctx.client_id)
    flash(request, "Rules saved.")
    return go(f"/dealers/{ctx.client_id}/automation")


@router.post("/dealers/{client_id}/automation/evaluate")
def run_eval(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    run = evaluation.run_and_record(ctx.db, user_id=ctx.actor_id, dealership_id=ctx.client_id)
    flash(request, f"Evaluation {'PASSED' if run.passed else 'FAILED'} ({run.total_cases} synthetic conversations).")
    return go(f"/dealers/{ctx.client_id}/automation")


@router.post("/dealers/{client_id}/automation/mode")
def set_mode(request: Request, mode: str = Form(...), statement_ack: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("respond"))):
    db, c = ctx.db, ctx.client
    url = f"/dealers/{c.id}/automation"
    if mode not in MODES:
        flash(request, "Unknown mode.", "error")
        return go(url)
    s = get_settings_row(db, c.id)
    if mode != MODE_DRAFT_ONLY:
        if not ctx.can("authorize_automation"):
            raise HTTPException(403, "Only this dealership's own admin can authorize automated replies")
        run = db.scalar(select(EvaluationRun).where(EvaluationRun.engine_version == ENGINE_VERSION, EvaluationRun.passed.is_(True))
                        .order_by(EvaluationRun.id.desc()))
        if run is None or (utcnow() - run.created_at).days > 30:
            flash(request, "Run a passing evaluation (within 30 days) before enabling this mode.", "error")
            return go(url)
        if statement_ack != "on":
            flash(request, "You must confirm the authorization statement.", "error")
            return go(url)
        statement = (f"I, {ctx.user.name or ctx.user.email} ({ctx.user.email}), authorize {mode} for {c.name}. Automated replies may only "
                     "use verified inventory data, dealer-approved information and approved templates, must escalate everything else, "
                     "and can be paused at any time.")
        db.add(ModeAuthorization(client_id=c.id, mode=mode, statement=statement, authorized_by_id=ctx.actor_id, evaluation_run_id=run.id))
    for a in db.scalars(select(ModeAuthorization).where(ModeAuthorization.mode != mode, ModeAuthorization.revoked_at.is_(None))):
        a.revoked_at, a.revoked_by_id = utcnow(), ctx.actor_id
    s.mode = mode
    audit.event(db, "automation.mode_changed", actor_id=ctx.actor_id, client_id=c.id, mode=mode)
    note = "" if mode == MODE_DRAFT_ONLY or automation_enabled(db, c.id, ONBOARDING_FEATURE) else (
        f" It stays draft-only until the agency enables '{ONBOARDING_FEATURE}' after onboarding.")
    flash(request, f"Mode set to {mode}.{note}")
    return go(url)


@router.post("/dealers/{client_id}/automation/chat-key")
def rotate_chat_key(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    s = get_settings_row(ctx.db, ctx.client_id)
    s.chat_widget_key = secrets.token_urlsafe(24)
    audit.event(ctx.db, "chat_widget.key_rotated", actor_id=ctx.actor_id, client_id=ctx.client_id)
    flash(request, "New website-chat key created; update the snippet on the dealership's site.")
    return go(f"/dealers/{ctx.client_id}/automation")


# --- notifications --------------------------------------------------------------------------------------------


@router.get("/dealers/{client_id}/notifications")
def notifications_page(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    db = ctx.db
    prefs = notify.get_prefs(db, ctx.client_id)
    mine = list(db.scalars(select(DealerNotification).where(DealerNotification.user_id == ctx.actor_id, DealerNotification.channel == "IN_APP")
                           .order_by(DealerNotification.id.desc()).limit(100)))
    for n in mine:
        n.read_at = n.read_at or utcnow()
    sent = list(db.scalars(select(DealerNotification).where(DealerNotification.channel == "EMAIL").order_by(DealerNotification.id.desc()).limit(50)))
    return page(request, ctx, "inbox_notifications.html", prefs=prefs, mine=mine, sent=sent, can_manage=ctx.can("manage"))


@router.post("/dealers/{client_id}/notifications")
def save_notifications(request: Request, immediate_alerts: str = Form(""), daily_digest: str = Form(""), digest_hour: int = Form(8),
                       notify_assigned: str = Form(""), email_enabled: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    prefs = notify.get_prefs(ctx.db, ctx.client_id)
    prefs.immediate_alerts = immediate_alerts == "on"
    prefs.daily_digest = daily_digest == "on"
    prefs.digest_hour_local = max(0, min(23, digest_hour))
    prefs.notify_assigned_user = notify_assigned == "on"
    prefs.email_enabled = email_enabled == "on"
    audit.event(ctx.db, "notifications.preferences_updated", actor_id=ctx.actor_id, client_id=ctx.client_id)
    flash(request, "Notification preferences saved.")
    return go(f"/dealers/{ctx.client_id}/notifications")
