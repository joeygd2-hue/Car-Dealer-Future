"""Dealership client portal. Every route resolves the tenant server-side (portal_scope*) and every
object lookup uses get_owned(...) so a user can never read or change another dealership's data."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.integrations import registry
from app.integrations.base import IntegrationError
from app.models import (
    FAQ,
    MODE_CONTROLLED,
    MODE_DRAFT_ONLY,
    MODES,
    Appointment,
    ApprovedTemplate,
    AuditLog,
    BusinessHours,
    Conversation,
    Escalation,
    EvaluationRun,
    IntegrationConnection,
    ListingDraft,
    Membership,
    Message,
    ModeAuthorization,
    Notification,
    ResponseDraft,
    User,
    Vehicle,
)
from app.security.auth import AuthContext, DealershipScope, get_auth, get_owned
from app.services import appointments as appt_svc
from app.services import audit, escalation, evaluation, notifications
from app.services.automation_policy import (
    CONTROLLABLE_INTENTS,
    effective_mode,
    get_settings_row,
    valid_mode_authorization,
)
from app.services.inbox.outbound import queue_outbound, record_manual_external
from app.services.inbox.processing import ingest_inbound
from app.services.responder.engine import ENGINE_VERSION
from app.services.summaries import build_summary
from app.web.deps import dealer_admin_post, portal_scope, portal_scope_post, redirect, render

router = APIRouter(prefix="/portal")

INTENT_CHOICES = ["greeting", "availability", "price", "mileage", "features", "hours", "location", "thanks"]


@router.get("")
def portal_home(request: Request, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    if auth.user.is_platform_admin:
        return RedirectResponse("/admin", status_code=303)
    memberships = list(db.scalars(select(Membership).where(Membership.user_id == auth.user.id)))
    if len(memberships) == 1:
        return RedirectResponse(f"/portal/{memberships[0].dealership_id}", status_code=303)
    return render(request, "portal/choose.html", db, memberships=memberships)


@router.get("/{dealership_id}")
def dashboard(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    d = scope.dealership
    q = lambda m, *w: db.scalar(select(func.count(m.id)).where(m.dealership_id == d.id, *w)) or 0  # noqa: E731
    stats = {
        "open_conversations": q(Conversation, Conversation.status.in_(["OPEN", "AWAITING_STAFF", "ESCALATED"])),
        "pending_drafts": q(ResponseDraft, ResponseDraft.status == "PENDING_REVIEW"),
        "open_escalations": q(Escalation, Escalation.status != "RESOLVED"),
        "pending_appointments": q(Appointment, Appointment.status.in_(["PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED"])),
        "listing_drafts": q(ListingDraft, ListingDraft.status == "DRAFT"),
        "vehicles": q(Vehicle),
    }
    settings = get_settings_row(db, d.id)
    recent = list(db.scalars(select(Conversation).where(Conversation.dealership_id == d.id)
                             .order_by(Conversation.updated_at.desc()).limit(8)))
    return render(request, "portal/dashboard.html", db, scope=scope, d=d, stats=stats, settings=settings,
                  mode=effective_mode(db, settings), recent=recent)


# ------------------------------------------------------------------------------------- inbox


@router.get("/{dealership_id}/inbox")
def inbox(request: Request, status: str | None = None, channel: str | None = None, mine: int = 0,
          scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    stmt = select(Conversation).where(Conversation.dealership_id == scope.dealership.id)
    if status:
        stmt = stmt.where(Conversation.status == status)
    if channel:
        stmt = stmt.where(Conversation.channel == channel)
    if mine:
        stmt = stmt.where(Conversation.assigned_user_id == scope.user.id)
    convs = list(db.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(200)))
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.dealership_id == scope.dealership.id))}
    return render(request, "portal/inbox.html", db, scope=scope, d=scope.dealership, convs=convs, vehicles=vehicles,
                  channels=registry.CUSTOMER_CHANNELS, f_status=status, f_channel=channel, mine=mine)


def _members(db: Session, dealership_id: int) -> list[User]:
    return [m.user for m in db.scalars(select(Membership).where(Membership.dealership_id == dealership_id))]


@router.get("/{dealership_id}/conversations/{conversation_id}")
def conversation_view(conversation_id: int, request: Request, scope: DealershipScope = Depends(portal_scope),
                      db: Session = Depends(get_db)):
    conv = get_owned(db, Conversation, conversation_id, scope.dealership.id)
    drafts = list(db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id)
                             .order_by(ResponseDraft.id.desc()).limit(10)))
    pending = next((x for x in drafts if x.status == "PENDING_REVIEW"), None)
    escs = list(db.scalars(select(Escalation).where(Escalation.conversation_id == conv.id).order_by(Escalation.id.desc())))
    appts = list(db.scalars(select(Appointment).where(Appointment.conversation_id == conv.id)))
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    return render(request, "portal/conversation.html", db, scope=scope, d=scope.dealership, conv=conv, drafts=drafts,
                  pending=pending, escalations=escs, appointments=appts, vehicle=vehicle,
                  members=_members(db, scope.dealership.id), sendable=conv.channel in registry.SENDABLE_CHANNELS,
                  vehicles=list(db.scalars(select(Vehicle).where(Vehicle.dealership_id == scope.dealership.id))))


@router.post("/{dealership_id}/conversations/{conversation_id}/send")
def send_reply(conversation_id: int, body: str = Form(...), draft_id: int | None = Form(None),
               manual_external: int = Form(0), scope: DealershipScope = Depends(portal_scope_post),
               db: Session = Depends(get_db)):
    conv = get_owned(db, Conversation, conversation_id, scope.dealership.id)
    url = f"/portal/{scope.dealership.id}/conversations/{conv.id}"
    body = body.strip()
    if not body:
        return redirect(url, err="Message is empty.")
    draft = get_owned(db, ResponseDraft, draft_id, scope.dealership.id) if draft_id else None
    if draft and draft.conversation_id != conv.id:
        raise HTTPException(404, "Not found")
    if conv.opted_out:
        return redirect(url, err="The customer opted out. Messages cannot be sent from the platform.")
    if draft:
        draft.reviewed_by_id, draft.reviewed_at = scope.user.id, utcnow()
        if draft.body != body:
            audit.record(db, "draft.edited", actor_type="USER", actor_user_id=scope.user.id,
                         dealership_id=scope.dealership.id, entity_type="response_draft", entity_id=draft.id)
    if conv.channel not in registry.SENDABLE_CHANNELS or manual_external:
        record_manual_external(db, conv, body, user_id=scope.user.id, draft=draft)
        build_summary(db, conv)
        db.commit()
        return redirect(url, msg="Recorded as sent outside the platform. Paste the reply into the original channel.")
    queue_outbound(db, conv, body, sender_type="STAFF", user_id=scope.user.id, draft=draft)
    if draft:
        draft.status = "SENT"
    if conv.assigned_user_id is None:
        conv.assigned_user_id = scope.user.id
    build_summary(db, conv)
    db.commit()
    return redirect(url, msg="Reply queued for delivery. Delivery status appears next to the message.")


@router.post("/{dealership_id}/conversations/{conversation_id}/drafts/{draft_id}/reject")
def reject_draft(conversation_id: int, draft_id: int, scope: DealershipScope = Depends(portal_scope_post),
                 db: Session = Depends(get_db)):
    draft = get_owned(db, ResponseDraft, draft_id, scope.dealership.id)
    if draft.conversation_id != conversation_id:
        raise HTTPException(404, "Not found")
    draft.status, draft.reviewed_by_id, draft.reviewed_at = "REJECTED", scope.user.id, utcnow()
    audit.record(db, "draft.rejected", actor_type="USER", actor_user_id=scope.user.id, dealership_id=scope.dealership.id,
                 entity_type="response_draft", entity_id=draft.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/conversations/{conversation_id}", msg="Draft rejected.")


@router.post("/{dealership_id}/conversations/{conversation_id}/assign")
def assign(conversation_id: int, user_id: int | None = Form(None), scope: DealershipScope = Depends(portal_scope_post),
           db: Session = Depends(get_db)):
    conv = get_owned(db, Conversation, conversation_id, scope.dealership.id)
    if user_id is not None and user_id not in {u.id for u in _members(db, scope.dealership.id)}:
        raise HTTPException(400, "User is not a member of this dealership")  # prevents cross-tenant assignment
    conv.assigned_user_id = user_id
    audit.record(db, "conversation.assigned", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id, entity_type="conversation", entity_id=conv.id, assignee=user_id)
    if user_id:
        notifications.notify(db, scope.dealership.id, kind="NEW_INQUIRY", subject=f"Conversation #{conv.id} assigned to you",
                             body="A customer conversation was assigned to you.",
                             link=f"/portal/{scope.dealership.id}/conversations/{conv.id}", assigned_user_id=user_id,
                             dedupe_key=f"assign:{conv.id}:{user_id}:{utcnow():%Y%m%d%H%M}")
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/conversations/{conv.id}", msg="Assignment updated.")


@router.post("/{dealership_id}/conversations/{conversation_id}/takeover")
def takeover(conversation_id: int, on: int = Form(...), scope: DealershipScope = Depends(portal_scope_post),
             db: Session = Depends(get_db)):
    conv = get_owned(db, Conversation, conversation_id, scope.dealership.id)
    conv.human_takeover = bool(on)
    conv.takeover_by_id = scope.user.id if on else None
    conv.takeover_at = utcnow() if on else None
    if not on:
        conv.human_requested = False
    audit.record(db, "conversation.takeover_on" if on else "conversation.takeover_off", actor_type="USER",
                 actor_user_id=scope.user.id, dealership_id=scope.dealership.id, entity_type="conversation", entity_id=conv.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/conversations/{conv.id}",
                    msg="Human takeover enabled: automation will not reply." if on else "Automation re-enabled for this conversation.")


@router.post("/{dealership_id}/conversations/{conversation_id}/vehicle")
def link_vehicle(conversation_id: int, vehicle_id: int | None = Form(None), scope: DealershipScope = Depends(portal_scope_post),
                 db: Session = Depends(get_db)):
    conv = get_owned(db, Conversation, conversation_id, scope.dealership.id)
    if vehicle_id:
        get_owned(db, Vehicle, vehicle_id, scope.dealership.id)
    conv.vehicle_id = vehicle_id
    build_summary(db, conv)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/conversations/{conv.id}", msg="Vehicle link updated.")


@router.post("/{dealership_id}/conversations/{conversation_id}/close")
def close_conv(conversation_id: int, scope: DealershipScope = Depends(portal_scope_post), db: Session = Depends(get_db)):
    conv = get_owned(db, Conversation, conversation_id, scope.dealership.id)
    conv.status = "CLOSED"
    audit.record(db, "conversation.closed", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id, entity_type="conversation", entity_id=conv.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/inbox", msg="Conversation closed.")


@router.post("/{dealership_id}/escalations/{escalation_id}/resolve")
def resolve_escalation(escalation_id: int, scope: DealershipScope = Depends(portal_scope_post), db: Session = Depends(get_db)):
    esc = get_owned(db, Escalation, escalation_id, scope.dealership.id)
    escalation.resolve(db, esc, scope.user.id)
    build_summary(db, db.get(Conversation, esc.conversation_id))
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/conversations/{esc.conversation_id}", msg="Escalation resolved.")


@router.post("/{dealership_id}/marketplace/intake")
def marketplace_intake(thread_label: str = Form(...), text: str = Form(...), vehicle_id: int | None = Form(None),
                       buyer_name: str = Form(""), scope: DealershipScope = Depends(portal_scope_post),
                       db: Session = Depends(get_db)):
    """Human-assisted Marketplace workflow: staff paste a buyer message they read in Facebook themselves."""
    if vehicle_id:
        get_owned(db, Vehicle, vehicle_id, scope.dealership.id)
    if thread_label.startswith("__existing__"):
        thread = thread_label.removeprefix("__existing__")
        if not db.scalar(select(Conversation.id).where(Conversation.dealership_id == scope.dealership.id,
                                                       Conversation.channel == registry.MARKETPLACE_ASSISTED,
                                                       Conversation.external_conversation_id == thread)):
            raise HTTPException(404, "Not found")
    else:
        thread = "mp:" + hashlib.sha256(thread_label.strip().lower().encode()).hexdigest()[:24]
    pid = hashlib.sha256(f"{thread}:{text.strip()}".encode()).hexdigest()[:40]  # duplicate paste → same id
    res = ingest_inbound(db, dealership=scope.dealership, channel=registry.MARKETPLACE_ASSISTED,
                         external_conversation_id=thread, text=text.strip(), provider_message_id=pid,
                         contact_hints={"name": buyer_name.strip() or None}, vehicle_id=vehicle_id)
    conv = res.conversation
    if conv.assigned_user_id is None:
        conv.assigned_user_id = scope.user.id
    audit.record(db, "marketplace.intake", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id, entity_type="conversation", entity_id=conv.id, duplicate=not res.created)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/conversations/{conv.id}",
                    msg="Message logged. A draft reply will appear shortly; copy it into Marketplace yourself."
                    if res.created else "This message was already logged.")


# ------------------------------------------------------------------------------- inventory


@router.get("/{dealership_id}/inventory")
def inventory(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    vehicles = list(db.scalars(select(Vehicle).where(Vehicle.dealership_id == scope.dealership.id).order_by(Vehicle.id.desc())))
    drafts = list(db.scalars(select(ListingDraft).where(ListingDraft.dealership_id == scope.dealership.id)
                             .order_by(ListingDraft.id.desc()).limit(100)))
    settings = get_settings_row(db, scope.dealership.id)
    return render(request, "portal/inventory.html", db, scope=scope, d=scope.dealership, vehicles=vehicles, drafts=drafts,
                  stale_hours=settings.stale_inventory_hours, now=utcnow())


@router.post("/{dealership_id}/inventory/vehicles")
def save_vehicle(vehicle_id: int | None = Form(None), year: int | None = Form(None), make: str = Form(""),
                 model: str = Form(""), trim: str = Form(""), vin: str = Form(""), stock_number: str = Form(""),
                 price: str = Form(""), mileage: str = Form(""), status: str = Form("AVAILABLE"), features: str = Form(""),
                 conflict_note: str = Form(""), scope: DealershipScope = Depends(portal_scope_post),
                 db: Session = Depends(get_db)):
    v = get_owned(db, Vehicle, vehicle_id, scope.dealership.id) if vehicle_id else Vehicle(dealership_id=scope.dealership.id)
    v.year, v.make, v.model, v.trim = year, make.strip() or None, model.strip() or None, trim.strip() or None
    v.vin, v.stock_number = vin.strip().upper() or None, stock_number.strip() or None
    v.price_cents = int(round(float(price.replace(",", "").replace("$", "")) * 100)) if price.strip() else None
    v.mileage = int(mileage.replace(",", "")) if mileage.strip() else None
    v.status = status if status in ("AVAILABLE", "PENDING", "SOLD", "UNKNOWN") else "UNKNOWN"
    v.approved_features = [f.strip() for f in features.split(",") if f.strip()]
    v.data_conflict_note = conflict_note.strip() or None
    v.source, v.source_updated_at, v.verified_at = "MANUAL", utcnow(), utcnow()
    db.add(v)
    db.flush()
    audit.record(db, "vehicle.saved", actor_type="USER", actor_user_id=scope.user.id, dealership_id=scope.dealership.id,
                 entity_type="vehicle", entity_id=v.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/inventory", msg="Vehicle saved and marked verified now.")


@router.post("/{dealership_id}/inventory/vehicles/{vehicle_id}/verify")
def verify_vehicle(vehicle_id: int, scope: DealershipScope = Depends(portal_scope_post), db: Session = Depends(get_db)):
    v = get_owned(db, Vehicle, vehicle_id, scope.dealership.id)
    v.verified_at = utcnow()
    audit.record(db, "vehicle.verified", actor_type="USER", actor_user_id=scope.user.id, dealership_id=scope.dealership.id,
                 entity_type="vehicle", entity_id=v.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/inventory", msg=f"{v.title}: price/availability confirmed as current.")


@router.post("/{dealership_id}/listings/{draft_id}/{action}")
def review_listing(draft_id: int, action: str, note: str = Form(""), scope: DealershipScope = Depends(portal_scope_post),
                   db: Session = Depends(get_db)):
    if action not in ("approve", "reject"):
        raise HTTPException(404, "Not found")
    ld = get_owned(db, ListingDraft, draft_id, scope.dealership.id)
    ld.status = "APPROVED" if action == "approve" else "REJECTED"
    ld.reviewed_by_id, ld.reviewed_at, ld.review_note = scope.user.id, utcnow(), note or None
    audit.record(db, f"listing.{action}d", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id, entity_type="listing_draft", entity_id=ld.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/inventory", msg=f"Listing draft {ld.status.lower()}.")


# ------------------------------------------------------------------------------- appointments


@router.get("/{dealership_id}/appointments")
def appointments(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    appts = list(db.scalars(select(Appointment).where(Appointment.dealership_id == scope.dealership.id)
                            .order_by(Appointment.requested_start.desc()).limit(200)))
    vehicles = {v.id: v for v in db.scalars(select(Vehicle).where(Vehicle.dealership_id == scope.dealership.id))}
    tz = scope.dealership.timezone
    return render(request, "portal/appointments.html", db, scope=scope, d=scope.dealership, appts=appts, vehicles=vehicles,
                  local=lambda dt: appt_svc.utc_to_local(dt, tz) if dt else None,
                  calendar_connected=registry.dealership_calendar(db, scope.dealership.id) is not None)


@router.post("/{dealership_id}/appointments/{appointment_id}/{action}")
def appointment_action(appointment_id: int, action: str, new_start: str = Form(""), note: str = Form(""),
                       scope: DealershipScope = Depends(portal_scope_post), db: Session = Depends(get_db)):
    appt = get_owned(db, Appointment, appointment_id, scope.dealership.id)
    url = f"/portal/{scope.dealership.id}/appointments"
    try:
        if action == "confirm":
            appt_svc.confirm(db, appt, user_id=scope.user.id)
            msg = "Appointment confirmed. Confirmation queued to the customer (see delivery status in the conversation)."
        elif action == "decline":
            appt_svc.decline(db, appt, user_id=scope.user.id, note=note or None)
            msg = "Request declined. Contact the customer to offer another time."
        elif action == "cancel":
            appt_svc.cancel(db, appt, actor=f"user:{scope.user.id}", user_id=scope.user.id, reason=note or None)
            msg = "Appointment cancelled."
        elif action == "reschedule":
            local = datetime.fromisoformat(new_start)
            appt_svc.reschedule(db, appt, appt_svc.local_to_utc(local, scope.dealership.timezone), user_id=scope.user.id)
            msg = "Moved to the new time; confirm it once the customer agrees."
        elif action == "complete":
            appt.status = "COMPLETED"
            msg = "Marked completed."
        elif action == "no-show":
            appt.status = "NO_SHOW"
            msg = "Marked no-show."
        else:
            raise HTTPException(404, "Not found")
    except (ValueError, IntegrationError) as exc:
        db.rollback()
        return redirect(url, err=f"Could not {action}: {exc}")
    db.commit()
    return redirect(url, msg=msg)


# ------------------------------------------------------------------------------- settings


@router.get("/{dealership_id}/settings/business")
def business_settings(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    hours = {h.weekday: h for h in scope.dealership.hours}
    return render(request, "portal/business.html", db, scope=scope, d=scope.dealership, hours=hours,
                  days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])


@router.post("/{dealership_id}/settings/business")
async def save_business(request: Request, scope: DealershipScope = Depends(dealer_admin_post), db: Session = Depends(get_db)):
    form = await request.form()
    d = scope.dealership
    for f in ("phone", "email", "website", "address_line1", "city", "state", "postal_code"):
        setattr(d, f, (str(form.get(f, "")).strip() or None))
    existing = {h.weekday: h for h in d.hours}
    for wd in range(7):
        h = existing.get(wd) or BusinessHours(dealership_id=d.id, weekday=wd)
        h.closed = form.get(f"closed_{wd}") == "on"
        h.open_time = str(form.get(f"open_{wd}") or "") or None
        h.close_time = str(form.get(f"close_{wd}") or "") or None
        if not h.closed and (not h.open_time or not h.close_time or h.open_time >= h.close_time):
            return redirect(f"/portal/{d.id}/settings/business", err=f"Invalid hours for day {wd + 1}.")
        db.add(h)
    approved = form.get("approve") == "on"
    d.business_info_approved = approved
    d.business_info_approved_at = utcnow() if approved else None
    d.business_info_approved_by_id = scope.user.id if approved else None
    audit.record(db, "business_info.updated", actor_type="USER", actor_user_id=scope.user.id, dealership_id=d.id,
                 approved=approved)
    db.commit()
    return redirect(f"/portal/{d.id}/settings/business",
                    msg="Saved. " + ("Approved for use in customer replies." if approved else "Not approved: the assistant will not share it."))


@router.get("/{dealership_id}/settings/content")
def content_settings(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    faqs = list(db.scalars(select(FAQ).where(FAQ.dealership_id == scope.dealership.id).order_by(FAQ.id.desc())))
    templates = list(db.scalars(select(ApprovedTemplate).where(ApprovedTemplate.dealership_id == scope.dealership.id)
                                .order_by(ApprovedTemplate.intent)))
    return render(request, "portal/content.html", db, scope=scope, d=scope.dealership, faqs=faqs, templates=templates,
                  intents=INTENT_CHOICES)


@router.post("/{dealership_id}/settings/faqs")
def save_faq(question: str = Form(...), answer: str = Form(...), keywords: str = Form(""), language: str = Form("en"),
             auto_send_allowed: str = Form(""), scope: DealershipScope = Depends(dealer_admin_post),
             db: Session = Depends(get_db)):
    faq = FAQ(dealership_id=scope.dealership.id, question=question.strip(), answer=answer.strip(),
              keywords=[k.strip() for k in keywords.split(",") if k.strip()], language="es" if language == "es" else "en",
              auto_send_allowed=auto_send_allowed == "on", approved_by_id=scope.user.id, approved_at=utcnow())
    db.add(faq)
    db.flush()
    audit.record(db, "faq.approved", actor_type="USER", actor_user_id=scope.user.id, dealership_id=scope.dealership.id,
                 entity_type="faq", entity_id=faq.id, auto_send=faq.auto_send_allowed)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/content", msg="FAQ approved.")


@router.post("/{dealership_id}/settings/faqs/{faq_id}/deactivate")
def deactivate_faq(faq_id: int, scope: DealershipScope = Depends(dealer_admin_post), db: Session = Depends(get_db)):
    faq = get_owned(db, FAQ, faq_id, scope.dealership.id)
    faq.active = False
    audit.record(db, "faq.deactivated", actor_type="USER", actor_user_id=scope.user.id, dealership_id=scope.dealership.id,
                 entity_type="faq", entity_id=faq.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/content", msg="FAQ deactivated.")


@router.post("/{dealership_id}/settings/templates")
def save_template(intent: str = Form(...), language: str = Form("en"), body: str = Form(...),
                  auto_send_allowed: str = Form(""), scope: DealershipScope = Depends(dealer_admin_post),
                  db: Session = Depends(get_db)):
    if intent not in INTENT_CHOICES:
        return redirect(f"/portal/{scope.dealership.id}/settings/content", err="Unsupported intent.")
    lang = "es" if language == "es" else "en"
    for old in db.scalars(select(ApprovedTemplate).where(ApprovedTemplate.dealership_id == scope.dealership.id,
                                                         ApprovedTemplate.intent == intent, ApprovedTemplate.language == lang)):
        old.active = False
    t = ApprovedTemplate(dealership_id=scope.dealership.id, intent=intent, language=lang, body=body.strip(),
                         auto_send_allowed=auto_send_allowed == "on", approved_by_id=scope.user.id, approved_at=utcnow())
    db.add(t)
    db.flush()
    audit.record(db, "template.approved", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id, entity_type="template", entity_id=t.id, intent=intent)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/content", msg="Template approved.")


@router.get("/{dealership_id}/settings/automation")
def automation_settings(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    d = scope.dealership
    settings = get_settings_row(db, d.id)
    conns = list(db.scalars(select(IntegrationConnection).where(IntegrationConnection.dealership_id == d.id)))
    latest_eval = db.scalar(select(EvaluationRun).where(EvaluationRun.engine_version == ENGINE_VERSION)
                            .order_by(EvaluationRun.id.desc()))
    auths = list(db.scalars(select(ModeAuthorization).where(ModeAuthorization.dealership_id == d.id)
                            .order_by(ModeAuthorization.id.desc())))
    db.commit()
    return render(request, "portal/automation.html", db, scope=scope, d=d, s=settings, conns=conns, modes=MODES,
                  mode=effective_mode(db, settings), latest_eval=latest_eval, auths=auths,
                  controllable=sorted(CONTROLLABLE_INTENTS),
                  valid_b=valid_mode_authorization(db, d.id, "B_FAQ_AUTOMATION"),
                  valid_c=valid_mode_authorization(db, d.id, MODE_CONTROLLED))


@router.post("/{dealership_id}/settings/automation/pause")
def pause_automation(on: int = Form(...), reason: str = Form(""), scope: DealershipScope = Depends(portal_scope_post),
                     db: Session = Depends(get_db)):
    """Any staff member may PAUSE immediately; only admins may resume."""
    s = get_settings_row(db, scope.dealership.id)
    if not on and not scope.is_admin:
        raise HTTPException(403, "Only a dealership administrator can resume automation")
    s.paused, s.paused_reason = bool(on), (reason or None) if on else None
    s.paused_by_id, s.paused_at = (scope.user.id, utcnow()) if on else (None, None)
    audit.record(db, "automation.paused" if on else "automation.resumed", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id, reason=reason)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/automation",
                    msg="Automation paused for this dealership." if on else "Automation resumed.")


@router.post("/{dealership_id}/settings/automation/channels/{connection_id}/pause")
def pause_channel(connection_id: int, on: int = Form(...), scope: DealershipScope = Depends(portal_scope_post),
                  db: Session = Depends(get_db)):
    conn = db.get(IntegrationConnection, connection_id)
    if conn is None or conn.dealership_id != scope.dealership.id:
        raise HTTPException(404, "Not found")
    if not on and not scope.is_admin:
        raise HTTPException(403, "Only a dealership administrator can resume a channel")
    conn.paused = bool(on)
    audit.record(db, "channel.paused" if on else "channel.resumed", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id, entity_type="integration", entity_id=conn.id, provider=conn.provider)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/automation", msg="Channel updated.")


@router.post("/{dealership_id}/settings/automation/limits")
def save_limits(max_conv: int = Form(...), max_dealer: int = Form(...), min_interval: int = Form(...),
                after_hours: str = Form(""), stale_hours: int = Form(...), threshold: float = Form(...),
                escalation_emails: str = Form(""), controlled_intents: list[str] = Form([]),
                scope: DealershipScope = Depends(dealer_admin_post), db: Session = Depends(get_db)):
    s = get_settings_row(db, scope.dealership.id)
    s.max_auto_replies_per_conversation_per_hour = max(0, min(max_conv, 20))
    s.max_auto_replies_per_dealership_per_hour = max(0, min(max_dealer, 500))
    s.min_seconds_between_auto_replies = max(5, min_interval)
    s.auto_reply_outside_business_hours = after_hours == "on"
    s.stale_inventory_hours = max(1, stale_hours)
    s.confidence_threshold = max(0.5, min(threshold, 1.0))
    s.escalation_emails = [e.strip() for e in escalation_emails.split(",") if "@" in e]
    s.controlled_intents = [i for i in controlled_intents if i in CONTROLLABLE_INTENTS]
    audit.record(db, "automation.limits_updated", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/automation", msg="Rules saved.")


@router.post("/{dealership_id}/settings/automation/evaluate")
def run_eval(scope: DealershipScope = Depends(dealer_admin_post), db: Session = Depends(get_db)):
    run = evaluation.run_and_record(db, user_id=scope.user.id, dealership_id=scope.dealership.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/automation",
                    msg=f"Evaluation {'PASSED' if run.passed else 'FAILED'} ({run.total_cases} synthetic conversations).")


@router.post("/{dealership_id}/settings/automation/mode")
def set_mode(mode: str = Form(...), statement_ack: str = Form(""), scope: DealershipScope = Depends(dealer_admin_post),
             db: Session = Depends(get_db)):
    d = scope.dealership
    url = f"/portal/{d.id}/settings/automation"
    if mode not in MODES:
        return redirect(url, err="Unknown mode.")
    s = get_settings_row(db, d.id)
    if mode != MODE_DRAFT_ONLY:
        run = db.scalar(select(EvaluationRun).where(EvaluationRun.dealership_id == d.id,
                                                    EvaluationRun.engine_version == ENGINE_VERSION,
                                                    EvaluationRun.passed.is_(True)).order_by(EvaluationRun.id.desc()))
        if run is None or (utcnow() - run.created_at).days > 30:
            return redirect(url, err="Run a passing evaluation (within 30 days) before enabling this mode.")
        if statement_ack != "on":
            return redirect(url, err="You must confirm the authorization statement.")
        statement = (f"I, {scope.user.name} ({scope.user.email}), authorize {mode} for {d.name}. Automated replies may "
                     "only use verified inventory data, dealer-approved information and approved templates, must escalate "
                     "everything else, and can be paused at any time.")
        db.add(ModeAuthorization(dealership_id=d.id, mode=mode, statement=statement, authorized_by_id=scope.user.id,
                                 evaluation_run_id=run.id))
    for a in db.scalars(select(ModeAuthorization).where(ModeAuthorization.dealership_id == d.id,
                                                        ModeAuthorization.mode != mode,
                                                        ModeAuthorization.revoked_at.is_(None))):
        a.revoked_at = utcnow()
    s.mode = mode
    audit.record(db, "automation.mode_changed", actor_type="USER", actor_user_id=scope.user.id, dealership_id=d.id, mode=mode)
    db.commit()
    return redirect(url, msg=f"Mode set to {mode}.")


@router.get("/{dealership_id}/settings/notifications")
def notification_settings(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    prefs = notifications.get_prefs(db, scope.dealership.id)
    members = list(db.scalars(select(Membership).where(Membership.dealership_id == scope.dealership.id)))
    sent = list(db.scalars(select(Notification).where(Notification.dealership_id == scope.dealership.id)
                           .order_by(Notification.id.desc()).limit(50)))
    db.commit()
    return render(request, "portal/notifications_settings.html", db, scope=scope, d=scope.dealership, prefs=prefs,
                  members=members, sent=sent)


@router.post("/{dealership_id}/settings/notifications")
def save_notifications(immediate_alerts: str = Form(""), daily_digest: str = Form(""), digest_hour: int = Form(8),
                       notify_assigned: str = Form(""), email_enabled: str = Form(""),
                       escalation_member_ids: list[int] = Form([]), scope: DealershipScope = Depends(dealer_admin_post),
                       db: Session = Depends(get_db)):
    prefs = notifications.get_prefs(db, scope.dealership.id)
    prefs.immediate_alerts = immediate_alerts == "on"
    prefs.daily_digest = daily_digest == "on"
    prefs.digest_hour_local = max(0, min(23, digest_hour))
    prefs.notify_assigned_user = notify_assigned == "on"
    prefs.email_enabled = email_enabled == "on"
    for m in db.scalars(select(Membership).where(Membership.dealership_id == scope.dealership.id)):
        m.receives_escalations = m.user_id in escalation_member_ids
    audit.record(db, "notifications.preferences_updated", actor_type="USER", actor_user_id=scope.user.id,
                 dealership_id=scope.dealership.id)
    db.commit()
    return redirect(f"/portal/{scope.dealership.id}/settings/notifications", msg="Notification preferences saved.")


@router.get("/{dealership_id}/notifications")
def my_notifications(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    items = list(db.scalars(select(Notification).where(Notification.user_id == scope.user.id,
                                                       Notification.dealership_id == scope.dealership.id,
                                                       Notification.channel == "IN_APP")
                            .order_by(Notification.id.desc()).limit(100)))
    for n in items:
        n.read_at = n.read_at or utcnow()
    db.commit()
    return render(request, "portal/notifications.html", db, scope=scope, d=scope.dealership, items=items)


@router.get("/{dealership_id}/audit")
def audit_view(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    logs = list(db.scalars(select(AuditLog).where(AuditLog.dealership_id == scope.dealership.id)
                           .order_by(AuditLog.id.desc()).limit(300)))
    users = {u.id: u for u in db.scalars(select(User))}
    return render(request, "portal/audit.html", db, scope=scope, d=scope.dealership, logs=logs, users=users)


@router.get("/{dealership_id}/account")
def account(request: Request, scope: DealershipScope = Depends(portal_scope), db: Session = Depends(get_db)):
    conns = list(db.scalars(select(IntegrationConnection).where(IntegrationConnection.dealership_id == scope.dealership.id)))
    usage = {
        "messages_30d": db.scalar(select(func.count(Message.id)).where(
            Message.dealership_id == scope.dealership.id, Message.created_at >= utcnow() - timedelta(days=30))) or 0,
    }
    return render(request, "portal/account.html", db, scope=scope, d=scope.dealership, conns=conns, usage=usage)
