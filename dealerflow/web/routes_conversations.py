"""Phase 3 dealer workspace: customer inbox, conversation review/reply, appointments, response settings.

Pages live under ``/dealers/{client_id}/…`` and the JSON API under ``/api/v1/dealers/{client_id}/…``. Both get a
``ClientContext`` only through Phase 2's ``dealer_ctx(permission)`` (server-side role check, tenant-scoped session);
another dealership's ids return 404. The work itself is in ``services/conversations/staff.py``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..config import get_settings
from ..models import (
    Appointment,
    AppointmentEvent,
    Conversation,
    ConversationEscalation,
    ConversationMessage,
    CustomerContact,
    DealerFAQ,
    ResponseDraft,
    ResponseEvaluationRun,
    ResponseTemplate,
    StaffNotification,
)
from ..models_conversations import CHANNELS, MODES, SENDABLE_CHANNELS
from ..models_dealer import Vehicle
from ..services.conversations import staff
from ..services.conversations.common import local_to_utc, utc_to_local
from ..services.conversations.outbound import live_delivery
from ..services.conversations.policy import (
    CONTROLLABLE_INTENTS,
    effective_mode,
    emergency_stopped,
    global_pause,
    settings_row,
)
from ..services.conversations.responder.facts import parse_hours, vehicle_title
from ..services.dealer_errors import BadRequest
from ..services.onboarding import automation_enabled
from ..tenancy import ClientContext
from .deps import check_csrf, flash
from .routes_dealers import dealer_ctx, go, page

router = APIRouter(dependencies=[Depends(check_csrf)])
api = APIRouter(prefix="/api/v1/dealers/{client_id}", dependencies=[Depends(check_csrf)], tags=["conversations"])

OPEN_STATUSES = ("open", "awaiting_staff", "escalated")
MODE_LABELS = {"a_draft_only": "A — draft only (staff send every reply)", "b_faq_automation": "B — approved FAQ/template answers",
               "c_controlled": "C — controlled automation"}


def _conv(ctx: ClientContext, conversation_id: int) -> Conversation:
    return staff.get_owned(ctx, Conversation, conversation_id)


def _messages(ctx: ClientContext, conv: Conversation) -> list[ConversationMessage]:
    return list(ctx.db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conv.id)
                               .order_by(ConversationMessage.id)))


def conversation_detail(ctx: ClientContext, conv: Conversation) -> dict:
    drafts = list(ctx.db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id)
                                 .order_by(ResponseDraft.id.desc()).limit(10)))
    appts = list(ctx.db.scalars(select(Appointment).where(Appointment.conversation_id == conv.id).order_by(Appointment.id)))
    return {
        "conv": conv, "messages": _messages(ctx, conv), "drafts": drafts,
        "pending": next((d for d in drafts if d.status == "pending_review"), None),
        "escalations": list(ctx.db.scalars(select(ConversationEscalation).where(ConversationEscalation.conversation_id == conv.id)
                                           .order_by(ConversationEscalation.id.desc()))),
        "appointments": appts,
        "contact": ctx.db.get(CustomerContact, conv.contact_id) if conv.contact_id else None,
        "vehicle": ctx.db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None,
        "live": live_delivery(ctx.db, conv), "sendable": conv.channel in SENDABLE_CHANNELS,
    }


# --- pages ------------------------------------------------------------------------------------------------------

@router.get("/dealers/{client_id}/inbox")
def inbox(request: Request, status: str = "", channel: str = "", mine: int = 0, ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Conversation)
    if status == "open":
        stmt = stmt.where(Conversation.status.in_(OPEN_STATUSES))
    elif status:
        stmt = stmt.where(Conversation.status == status)
    if channel:
        stmt = stmt.where(Conversation.channel == channel)
    if mine and ctx.user:
        stmt = stmt.where(Conversation.assigned_user_id == ctx.user.id)
    convs = list(ctx.db.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(200)))
    pending = dict(ctx.db.execute(select(ResponseDraft.conversation_id, func.count()).where(ResponseDraft.status == "pending_review")
                                  .group_by(ResponseDraft.conversation_id)).all())
    contacts = {c.id: c for c in ctx.db.scalars(select(CustomerContact).where(
        CustomerContact.id.in_([c.contact_id for c in convs if c.contact_id])))}
    vehicles = {v.id: vehicle_title(v) for v in ctx.db.scalars(select(Vehicle).where(
        Vehicle.id.in_([c.vehicle_id for c in convs if c.vehicle_id])))}
    notes = list(ctx.db.scalars(select(StaffNotification).where(
        StaffNotification.channel == "in_app", StaffNotification.user_id == (ctx.user.id if ctx.user else -1),
        StaffNotification.read_at.is_(None)).order_by(StaffNotification.id.desc()).limit(20)))
    s = settings_row(ctx.db, ctx.client_id)
    return page(request, ctx, "dealer_inbox.html", convs=convs, pending=pending, contacts=contacts, vehicles=vehicles,
                notes=notes, f_status=status, f_channel=channel, mine=mine, channels=CHANNELS, mode=effective_mode(ctx.db, s),
                mode_labels=MODE_LABELS, rs=s, stopped=emergency_stopped(ctx.db), paused_all=global_pause(ctx.db),
                stock=list(ctx.db.scalars(select(Vehicle).where(Vehicle.inventory_status.in_(("available", "pending")))
                                          .order_by(Vehicle.year.desc()).limit(300))), vehicle_title=vehicle_title)


@router.post("/dealers/{client_id}/notifications/read")
def notifications_read(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    from ..db import utcnow

    for n in ctx.db.scalars(select(StaffNotification).where(StaffNotification.user_id == (ctx.user.id if ctx.user else -1),
                                                            StaffNotification.read_at.is_(None))):
        n.read_at = utcnow()
    return go(f"/dealers/{ctx.client_id}/inbox")


@router.get("/dealers/{client_id}/conversations/{conversation_id}")
def conversation_page(request: Request, conversation_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    d = conversation_detail(ctx, _conv(ctx, conversation_id))
    return page(request, ctx, "dealer_conversation.html", **d, members=staff.members(ctx), vehicle_title=vehicle_title,
                stock=list(ctx.db.scalars(select(Vehicle).where(Vehicle.inventory_status.in_(("available", "pending", "sold")))
                                          .order_by(Vehicle.year.desc()).limit(300))),
                send_warnings=[], unsent_body="", local=lambda dt: utc_to_local(dt, ctx.client.timezone) if dt else None)


@router.post("/dealers/{client_id}/conversations/{conversation_id}/send")
def conversation_send(request: Request, conversation_id: int, body: str = Form(...), draft_id: str = Form(""),
                      manual_external: str = Form(""), acknowledge_warnings: str = Form(""),
                      ctx: ClientContext = Depends(dealer_ctx("converse"))):
    conv = _conv(ctx, conversation_id)
    res = staff.send_reply(ctx, conv, body, draft_id=int(draft_id) if draft_id.isdigit() else None,
                           acknowledge_warnings=acknowledge_warnings == "yes", manual_external=manual_external == "yes")
    if res["status"] == "needs_acknowledgement":
        d = conversation_detail(ctx, conv)
        return page(request, ctx, "dealer_conversation.html", **d, members=staff.members(ctx), vehicle_title=vehicle_title,
                    stock=[], send_warnings=res["warnings"], unsent_body=body, draft_id=draft_id,
                    local=lambda dt: utc_to_local(dt, ctx.client.timezone) if dt else None)
    flash(request, "Recorded as sent outside the platform. Paste the reply into the original channel yourself."
          if res["status"] == "manual_external" else "Reply approved and queued. Its delivery status appears next to the message.")
    return go(f"/dealers/{ctx.client_id}/conversations/{conv.id}")


@router.post("/dealers/{client_id}/conversations/{conversation_id}/drafts/{draft_id}/reject")
def draft_reject(request: Request, conversation_id: int, draft_id: int, ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.reject_draft(ctx, _conv(ctx, conversation_id), draft_id)
    flash(request, "Draft rejected.")
    return go(f"/dealers/{ctx.client_id}/conversations/{conversation_id}")


@router.post("/dealers/{client_id}/conversations/{conversation_id}/assign")
def conversation_assign(request: Request, conversation_id: int, user_id: str = Form(""),
                        ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.assign(ctx, _conv(ctx, conversation_id), int(user_id) if user_id.isdigit() else None)
    flash(request, "Assignment updated.")
    return go(f"/dealers/{ctx.client_id}/conversations/{conversation_id}")


@router.post("/dealers/{client_id}/conversations/{conversation_id}/takeover")
def conversation_takeover(request: Request, conversation_id: int, on: str = Form(...),
                          ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.takeover(ctx, _conv(ctx, conversation_id), on == "1")
    flash(request, "Staff took over: automation will not reply." if on == "1" else "Automation re-enabled for this conversation.")
    return go(f"/dealers/{ctx.client_id}/conversations/{conversation_id}")


@router.post("/dealers/{client_id}/conversations/{conversation_id}/vehicle")
def conversation_vehicle(request: Request, conversation_id: int, vehicle_id: str = Form(""),
                         ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.link_vehicle(ctx, _conv(ctx, conversation_id), int(vehicle_id) if vehicle_id.isdigit() else None)
    flash(request, "Vehicle link updated.")
    return go(f"/dealers/{ctx.client_id}/conversations/{conversation_id}")


@router.post("/dealers/{client_id}/conversations/{conversation_id}/close")
def conversation_close(request: Request, conversation_id: int, ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.close(ctx, _conv(ctx, conversation_id))
    flash(request, "Conversation closed.")
    return go(f"/dealers/{ctx.client_id}/inbox")


@router.post("/dealers/{client_id}/conversations/{conversation_id}/messages/{message_id}/reconcile")
def message_reconcile(request: Request, conversation_id: int, message_id: int, delivered: str = Form(...),
                      ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.reconcile(ctx, message_id, delivered == "1")
    flash(request, "Marked delivered." if delivered == "1" else "Re-queued for delivery.")
    return go(f"/dealers/{ctx.client_id}/conversations/{conversation_id}")


@router.post("/dealers/{client_id}/escalations/{escalation_id}/resolve")
def escalation_resolve(request: Request, escalation_id: int, ctx: ClientContext = Depends(dealer_ctx("converse"))):
    esc = staff.resolve_escalation(ctx, escalation_id)
    flash(request, "Escalation resolved.")
    return go(f"/dealers/{ctx.client_id}/conversations/{esc.conversation_id}")


@router.post("/dealers/{client_id}/marketplace/intake")
def marketplace_intake(request: Request, thread_label: str = Form(...), text: str = Form(...), vehicle_id: str = Form(""),
                       buyer_name: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("converse"))):
    res = staff.marketplace_intake(ctx, thread_label=thread_label, text=text, buyer_name=buyer_name,
                                   vehicle_id=int(vehicle_id) if vehicle_id.isdigit() else None)
    flash(request, "Message logged. A draft reply will appear shortly; copy it into Marketplace yourself."
          if res.created else "This message was already logged.")
    return go(f"/dealers/{ctx.client_id}/conversations/{res.conversation.id}")


@router.get("/dealers/{client_id}/appointments")
def appointments_page(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    appts = list(ctx.db.scalars(select(Appointment).order_by(Appointment.id.desc()).limit(300)))
    events = {}
    for e in ctx.db.scalars(select(AppointmentEvent).where(AppointmentEvent.appointment_id.in_([a.id for a in appts]))
                            .order_by(AppointmentEvent.id)):
        events.setdefault(e.appointment_id, []).append(e)
    vehicles = {v.id: vehicle_title(v) for v in ctx.db.scalars(select(Vehicle).where(
        Vehicle.id.in_([a.vehicle_id for a in appts if a.vehicle_id])))}
    return page(request, ctx, "dealer_appointments.html", appts=appts, events=events, vehicles=vehicles,
                local=lambda dt: utc_to_local(dt, ctx.client.timezone) if dt else None)


@router.post("/dealers/{client_id}/appointments/{appointment_id}/{action}")
def appointment_action(request: Request, appointment_id: int, action: str, start_local: str = Form(""), note: str = Form(""),
                       ctx: ClientContext = Depends(dealer_ctx("converse"))):
    start = None
    if start_local.strip():
        try:
            start = local_to_utc(datetime.fromisoformat(start_local.strip()), ctx.client.timezone)
        except ValueError as exc:
            raise BadRequest("Use the date/time picker for the confirmed time") from exc
    staff.appointment_action(ctx, appointment_id, action, start_utc=start, note=note)
    flash(request, {"confirm": "Test drive confirmed. A confirmation is queued for the customer (if the channel allows it).",
                    "decline": "Request declined.", "cancel": "Appointment cancelled."}.get(action, "Appointment updated."))
    return go(f"/dealers/{ctx.client_id}/appointments")


@router.get("/dealers/{client_id}/responses")
def responses_page(request: Request, ctx: ClientContext = Depends(dealer_ctx())):
    s = settings_row(ctx.db, ctx.client_id)
    runs = list(ctx.db.scalars(select(ResponseEvaluationRun).where(ResponseEvaluationRun.client_id == ctx.client_id)
                               .order_by(ResponseEvaluationRun.id.desc()).limit(5)))
    return page(request, ctx, "dealer_responses.html", rs=s, base_url=get_settings().base_url.rstrip("/"), mode=effective_mode(ctx.db, s), modes=MODES,
                mode_labels=MODE_LABELS, hours=parse_hours(ctx.client.business_hours), runs=runs,
                faqs=list(ctx.db.scalars(select(DealerFAQ).where(DealerFAQ.active.is_(True)).order_by(DealerFAQ.id))),
                templates=list(ctx.db.scalars(select(ResponseTemplate).where(ResponseTemplate.active.is_(True))
                                              .order_by(ResponseTemplate.intent))),
                template_intents=staff.TEMPLATE_INTENTS, controllable=sorted(CONTROLLABLE_INTENTS),
                gate=automation_enabled(ctx.db, ctx.client_id, "inquiry_auto_response"),
                days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])


@router.post("/dealers/{client_id}/responses/business")
async def responses_business(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    f = await request.form()
    hours: dict[str, list[str] | None] = {}
    for wd in range(7):
        o, c = str(f.get(f"open_{wd}", "")).strip(), str(f.get(f"close_{wd}", "")).strip()
        hours[str(wd)] = [o, c] if o and c and f.get(f"closed_{wd}") != "yes" else None
    staff.save_business_info(ctx, phone=str(f.get("phone", "")), address_line=str(f.get("address_line", "")),
                             city=str(f.get("city", "")), state=str(f.get("state", "")), postal_code=str(f.get("postal_code", "")),
                             hours=hours, approve=f.get("approve") == "yes")
    flash(request, "Business details saved" + (" and approved for customer replies." if f.get("approve") == "yes"
                                               else ". They are not used in replies until approved."))
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/keys")
def responses_keys(request: Request, rotate: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    staff.ensure_public_keys(ctx, rotate=rotate == "yes")
    flash(request, "Intake keys rotated: update the website widget and ADF sender." if rotate == "yes" else "Intake keys created.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/faqs")
def responses_faq(request: Request, language: str = Form(...), question: str = Form(...), answer: str = Form(...),
                  keywords: str = Form(""), auto_send: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    staff.add_faq(ctx, language=language, question=question, answer=answer, keywords=keywords, auto_send=auto_send == "yes")
    flash(request, "FAQ approved.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/faqs/{faq_id}/deactivate")
def responses_faq_off(request: Request, faq_id: int, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    staff.deactivate_faq(ctx, faq_id)
    flash(request, "FAQ deactivated.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/templates")
def responses_template(request: Request, intent: str = Form(...), language: str = Form(...), body: str = Form(...),
                       auto_send: str = Form(""), ctx: ClientContext = Depends(dealer_ctx("manage"))):
    staff.save_template(ctx, intent=intent, language=language, body=body, auto_send=auto_send == "yes")
    flash(request, "Template approved.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/pause")
def responses_pause(request: Request, paused: str = Form(...), reason: str = Form(""),
                    ctx: ClientContext = Depends(dealer_ctx("manage"))):
    staff.set_pause(ctx, paused == "1", reason)
    flash(request, "Automation paused for this dealership." if paused == "1" else "Automation resumed.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/limits")
async def responses_limits(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    f = await request.form()
    try:
        values = {k: f[k] for k in ("max_auto_replies_per_conversation_per_hour", "max_auto_replies_per_dealership_per_hour",
                                    "min_seconds_between_auto_replies", "stale_inventory_hours", "confidence_threshold") if f.get(k)}
        values["auto_reply_outside_business_hours"] = f.get("auto_reply_outside_business_hours") == "yes"
        staff.set_limits(ctx, **values)
    except ValueError as exc:
        raise BadRequest("Limits must be numbers") from exc
    flash(request, "Limits saved.")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/evaluate")
def responses_evaluate(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    run = staff.run_evaluation(ctx)
    flash(request, f"Evaluation #{run.id}: {'passed' if run.passed else 'FAILED'} ({run.total_cases} synthetic conversations).",
          "info" if run.passed else "error")
    return go(f"/dealers/{ctx.client_id}/responses")


@router.post("/dealers/{client_id}/responses/mode")
async def responses_mode(request: Request, ctx: ClientContext = Depends(dealer_ctx("manage"))):
    f = await request.form()
    run_id = str(f.get("evaluation_run_id", ""))
    staff.set_mode(ctx, str(f.get("mode", "")), statement=str(f.get("statement", "")),
                   evaluation_run_id=int(run_id) if run_id.isdigit() else None,
                   controlled_intents=[str(x) for x in f.getlist("controlled_intents")])
    flash(request, "Response mode saved. Automatic sending also needs the onboarding gate to be enabled by agency staff.")
    return go(f"/dealers/{ctx.client_id}/responses")


# --- JSON API ---------------------------------------------------------------------------------------------------

def _conv_json(c: Conversation, pending: int = 0) -> dict:
    return {"id": c.id, "channel": c.channel, "status": c.status, "language": c.preferred_language, "vehicle_id": c.vehicle_id,
            "lead_id": c.lead_id, "contact_id": c.contact_id, "assigned_user_id": c.assigned_user_id, "opted_out": c.opted_out,
            "escalation_status": c.escalation_status, "appointment_status": c.appointment_status, "simulated": c.simulated,
            "pending_drafts": pending, "summary": c.summary, "created_at": c.created_at.isoformat()}


@api.get("/conversations")
def api_conversations(status: str = "", ctx: ClientContext = Depends(dealer_ctx())):
    stmt = select(Conversation)
    if status:
        stmt = stmt.where(Conversation.status == status)
    pending = dict(ctx.db.execute(select(ResponseDraft.conversation_id, func.count()).where(ResponseDraft.status == "pending_review")
                                  .group_by(ResponseDraft.conversation_id)).all())
    return [_conv_json(c, pending.get(c.id, 0)) for c in ctx.db.scalars(stmt.order_by(Conversation.id.desc()).limit(500))]


@api.get("/conversations/{conversation_id}")
def api_conversation(conversation_id: int, ctx: ClientContext = Depends(dealer_ctx())):
    d = conversation_detail(ctx, _conv(ctx, conversation_id))
    out = _conv_json(d["conv"])
    out["messages"] = [{"id": m.id, "direction": m.direction, "sender_type": m.sender_type, "body": m.body, "language": m.language,
                        "delivery_status": m.delivery_status, "delivery_error": m.delivery_error,
                        "created_at": m.created_at.isoformat()} for m in d["messages"]]
    out["drafts"] = [{"id": x.id, "status": x.status, "body": x.body, "language": x.language, "decision": x.decision,
                      "decision_reasons": x.decision_reasons, "escalation_reasons": x.escalation_reasons,
                      "facts_used": x.facts_used, "unresolved": x.unresolved, "confidence": x.confidence,
                      "generated_by": x.generated_by, "is_draft": x.status == "pending_review"} for x in d["drafts"]]
    out["escalations"] = [{"id": e.id, "status": e.status, "reasons": e.reasons, "recommended_action": e.recommended_action}
                          for e in d["escalations"]]
    out["appointments"] = [{"id": a.id, "status": a.status, "requested_start": a.requested_start.isoformat() if a.requested_start else None,
                            "confirmed_start": a.confirmed_start.isoformat() if a.confirmed_start else None}
                           for a in d["appointments"]]
    out["delivery"] = {"live": d["live"][0], "note": d["live"][1]}
    return out


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    draft_id: int | None = None
    acknowledge_warnings: bool = False
    manual_external: bool = False


@api.post("/conversations/{conversation_id}/reply")
def api_reply(conversation_id: int, payload: ReplyIn, ctx: ClientContext = Depends(dealer_ctx("converse"))):
    return staff.send_reply(ctx, _conv(ctx, conversation_id), payload.body, draft_id=payload.draft_id,
                            acknowledge_warnings=payload.acknowledge_warnings, manual_external=payload.manual_external)


@api.post("/conversations/{conversation_id}/drafts/{draft_id}/reject")
def api_reject(conversation_id: int, draft_id: int, ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.reject_draft(ctx, _conv(ctx, conversation_id), draft_id)
    return {"ok": True}


class TakeoverIn(BaseModel):
    on: bool


@api.post("/conversations/{conversation_id}/takeover")
def api_takeover(conversation_id: int, payload: TakeoverIn, ctx: ClientContext = Depends(dealer_ctx("converse"))):
    staff.takeover(ctx, _conv(ctx, conversation_id), payload.on)
    return {"ok": True}


@api.get("/appointments")
def api_appointments(ctx: ClientContext = Depends(dealer_ctx())):
    return [{"id": a.id, "conversation_id": a.conversation_id, "status": a.status, "kind": a.kind,
             "requested_start": a.requested_start.isoformat() if a.requested_start else None,
             "confirmed_start": a.confirmed_start.isoformat() if a.confirmed_start else None,
             "availability_checked": a.availability_checked}
            for a in ctx.db.scalars(select(Appointment).order_by(Appointment.id.desc()).limit(500))]


class AppointmentIn(BaseModel):
    start_utc: datetime | None = None
    note: str = ""


@api.post("/appointments/{appointment_id}/{action}")
def api_appointment_action(appointment_id: int, action: str, payload: AppointmentIn,
                           ctx: ClientContext = Depends(dealer_ctx("converse"))):
    a = staff.appointment_action(ctx, appointment_id, action, start_utc=payload.start_utc.replace(tzinfo=None)
                                 if payload.start_utc else None, note=payload.note)
    return {"id": a.id, "status": a.status}


class SimulatedInquiryIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    visitor_id: str = Field(min_length=8, max_length=64)
    name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    vehicle_stock: str | None = Field(default=None, max_length=50)


@api.post("/conversations/simulated-inquiry", status_code=201)
def api_simulated_inquiry(payload: SimulatedInquiryIn, ctx: ClientContext = Depends(dealer_ctx("converse"))):
    """Staff test tool: inject a SIMULATED website inquiry (marked simulated; excluded from real reports)."""
    from ..services.conversations.public import submit_chat

    res = submit_chat(ctx.db, ctx.client, visitor=payload.visitor_id, text=payload.text, client_message_id=None,
                      hints={"name": payload.name, "email": payload.email, "phone": payload.phone},
                      vehicle_stock=payload.vehicle_stock, simulated=True)
    return {"conversation_id": res.conversation.id, "message_id": res.message.id, "duplicate": not res.created, "simulated": True}
