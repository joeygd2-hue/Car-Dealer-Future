"""Customer test-drive appointments (ported from Phase 3).

A request is never presented to the customer as confirmed until a staff member confirms it. No dealership
calendar integration exists yet, so availability is recorded as ``no_calendar`` and staff check their own
calendar before confirming. Confirmation, reminder and cancellation messages go through the normal outbound
pipeline (so opt-outs, pauses and the emergency stop apply) and are re-validated at send time.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...jobs import enqueue, handler
from ...models import Appointment, AppointmentEvent, Client, Conversation, CustomerLead, User
from ...models_conversations import SENDABLE_CHANNELS
from ...models_dealer import Vehicle
from . import notify
from .common import log, local_to_utc, utc_to_local
from .responder.engine import describe_when
from .responder.facts import vehicle_title

ACTIVE = ("requested", "pending_confirmation", "confirmed", "reschedule_requested")
PENDING = ("requested", "pending_confirmation", "reschedule_requested")

CUSTOMER_TEXT = {
    "confirmation": {"en": "Your test drive at {dealer} is confirmed for {when}. Reply here if you need to make a change.",
                     "es": "Su prueba de manejo en {dealer} está confirmada para el {when}. Responda aquí si necesita hacer algún cambio."},
    "reminder": {"en": "Reminder: your test drive at {dealer} is scheduled for {when}.",
                 "es": "Recordatorio: su prueba de manejo en {dealer} está programada para el {when}."},
    "cancellation": {"en": "Your test drive at {dealer} for {when} has been cancelled. Reply here if you'd like a new time.",
                     "es": "Su prueba de manejo en {dealer} para el {when} fue cancelada. Responda aquí si desea otra hora."},
}


def _event(db: Session, appt: Appointment, event: str, actor: str, **details) -> None:
    db.add(AppointmentEvent(client_id=appt.client_id, appointment_id=appt.id, event=event, actor=actor, details=details))


def _sync_conv(db: Session, appt: Appointment) -> None:
    if appt.conversation_id:
        conv = db.get(Conversation, appt.conversation_id)
        if conv:
            conv.appointment_status = appt.status
            from .summaries import build_summary

            build_summary(db, conv)


def request_from_conversation(db: Session, conv: Conversation, start_local: datetime | None, *, name: str | None,
                              phone: str | None, email: str | None) -> Appointment:
    """Record the customer's request (``start_local`` None = test drive wanted, time not proposed yet)."""
    client = db.get(Client, conv.client_id)
    start = local_to_utc(start_local, client.timezone) if start_local else None
    existing = db.scalar(select(Appointment).where(Appointment.client_id == conv.client_id,
                                                   Appointment.conversation_id == conv.id, Appointment.status.in_(ACTIVE))
                         .order_by(Appointment.id.desc()))
    if existing and (existing.requested_start == start or start is None):
        return existing
    if existing and existing.status != "confirmed":
        existing.status = "cancelled"
        _event(db, existing, "superseded", "customer", new_start=start.isoformat() if start else None)
    appt = Appointment(client_id=conv.client_id, conversation_id=conv.id, contact_id=conv.contact_id, vehicle_id=conv.vehicle_id,
                       requested_start=start, customer_name=name, customer_phone=phone, customer_email=email,
                       status="pending_confirmation", availability_checked="no_calendar")
    db.add(appt)
    db.flush()
    _event(db, appt, "requested", "customer", start_local=start_local.isoformat() if start_local else None)
    _event(db, appt, "availability_checked", "system", result="no_calendar")
    conv.appointment_status, conv.lead_qualified = "pending_confirmation", True
    if conv.lead_id:
        lead = db.get(CustomerLead, conv.lead_id)
        if lead and lead.status == "new":
            lead.status = "appointment"
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    when = describe_when(start_local, "en") if start_local else "a time still to be agreed"
    log(db, "appointment.requested", client_id=conv.client_id, entity_type="appointment", entity_id=appt.id,
        actor_type="customer", conversation_id=conv.id, start=start.isoformat() if start else None)
    notify.notify(db, conv.client_id, kind="appointment", subject=f"Test drive request: {when}",
                  body=(f"A customer asked for a test drive{' of the ' + vehicle_title(vehicle) if vehicle else ''} for {when}. "
                        "It is NOT confirmed: check your calendar, then confirm or decline it."),
                  link=f"/dealers/{conv.client_id}/appointments", assigned_user_id=conv.assigned_user_id,
                  dedupe_key=f"appt-req:{appt.id}")
    return appt


def confirm(db: Session, appt: Appointment, *, user: User, start_utc: datetime | None = None) -> Appointment:
    if appt.status not in PENDING:
        raise ValueError(f"cannot confirm an appointment that is {appt.status}")
    start = start_utc or appt.requested_start
    if start is None:
        raise ValueError("choose the confirmed date and time first")
    appt.status, appt.confirmed_start = "confirmed", start
    appt.assigned_user_id = appt.assigned_user_id or user.id
    _event(db, appt, "confirmed", f"user:{user.id}", start=start.isoformat())
    log(db, "appointment.confirmed", client_id=appt.client_id, entity_type="appointment", entity_id=appt.id,
        actor_id=user.id, actor_type="user", start=start.isoformat())
    _customer_message(db, appt, "confirmation")
    _schedule_reminders(db, appt)
    _sync_conv(db, appt)
    return appt


def decline(db: Session, appt: Appointment, *, user: User, note: str | None = None) -> None:
    if appt.status not in PENDING:
        raise ValueError(f"cannot decline an appointment that is {appt.status}")
    appt.status, appt.notes = "declined", note
    _event(db, appt, "declined", f"user:{user.id}", note=note)
    log(db, "appointment.declined", client_id=appt.client_id, entity_type="appointment", entity_id=appt.id,
        actor_id=user.id, actor_type="user")
    _sync_conv(db, appt)


def cancel(db: Session, appt: Appointment, *, user: User, reason: str | None = None) -> None:
    if appt.status not in ACTIVE:
        raise ValueError(f"cannot cancel an appointment that is {appt.status}")
    was_confirmed = appt.status == "confirmed"
    appt.status = "cancelled"
    _event(db, appt, "cancelled", f"user:{user.id}", reason=reason)
    log(db, "appointment.cancelled", client_id=appt.client_id, entity_type="appointment", entity_id=appt.id,
        actor_id=user.id, actor_type="user")
    if was_confirmed:
        _customer_message(db, appt, "cancellation")
    _sync_conv(db, appt)


def complete(db: Session, appt: Appointment, *, user: User, outcome: str) -> None:
    if appt.status != "confirmed" or outcome not in ("completed", "no_show"):
        raise ValueError("only a confirmed appointment can be marked completed or no-show")
    appt.status = outcome
    _event(db, appt, outcome, f"user:{user.id}")
    log(db, f"appointment.{outcome}", client_id=appt.client_id, entity_type="appointment", entity_id=appt.id,
        actor_id=user.id, actor_type="user")
    _sync_conv(db, appt)


def customer_change_requested(db: Session, conv: Conversation, text: str) -> Appointment | None:
    """The customer asked to cancel/reschedule. Staff decide; nothing is cancelled or moved automatically."""
    appt = db.scalar(select(Appointment).where(Appointment.client_id == conv.client_id, Appointment.conversation_id == conv.id,
                                               Appointment.status.in_(ACTIVE)).order_by(Appointment.id.desc()))
    if appt is None:
        return None
    appt.status = "reschedule_requested"
    _event(db, appt, "customer_change_requested", "customer")
    log(db, "appointment.customer_change_requested", client_id=appt.client_id, entity_type="appointment", entity_id=appt.id,
        actor_type="customer")
    _sync_conv(db, appt)
    return appt


def _customer_message(db: Session, appt: Appointment, kind: str) -> None:
    conv = db.get(Conversation, appt.conversation_id) if appt.conversation_id else None
    if conv is None or conv.opted_out or conv.channel not in SENDABLE_CHANNELS:
        _event(db, appt, f"{kind}_not_sent", "system",
               reason="opted out" if conv and conv.opted_out else "channel requires a manual staff message")
        return
    client = db.get(Client, appt.client_id)
    start = appt.confirmed_start or appt.requested_start
    when = describe_when(utc_to_local(start, client.timezone), conv.preferred_language)
    body = CUSTOMER_TEXT[kind][conv.preferred_language].format(dealer=client.name, when=when)
    from .outbound import queue_outbound

    msg = queue_outbound(db, conv, body, sender_type="system", idempotency_key=f"appt:{appt.id}:{kind}:{start.isoformat()}")
    _event(db, appt, f"{kind}_queued", "system", message_id=msg.id)


def _schedule_reminders(db: Session, appt: Appointment) -> None:
    start = appt.confirmed_start
    for label, delta in (("24h", timedelta(hours=24)), ("2h", timedelta(hours=2))):
        run_at = start - delta
        if run_at > utcnow():
            enqueue(db, "appointment_reminder", f"appointment_reminder:{appt.id}:{start.isoformat()}:{label}",
                    {"appointment_id": appt.id, "start": start.isoformat()}, delay=run_at - utcnow(), max_attempts=3)


@handler("appointment_reminder")
def reminder_job(db: Session, payload: dict) -> dict:
    appt = db.get(Appointment, payload["appointment_id"])
    if appt is None or appt.status != "confirmed" or not appt.confirmed_start \
            or appt.confirmed_start.isoformat() != payload.get("start"):
        return {"skipped": "appointment changed"}
    if appt.confirmed_start < utcnow():
        return {"skipped": "in the past"}
    _customer_message(db, appt, "reminder")
    return {"queued": True}


@handler("appointment_reminder_sweep")
def reminder_sweep_job(db: Session, payload: dict) -> dict:
    """Recovery sweep: every upcoming confirmed appointment has its reminder jobs (keys make this idempotent)."""
    n = 0
    for appt in db.scalars(select(Appointment).where(Appointment.status == "confirmed", Appointment.confirmed_start > utcnow(),
                                                     Appointment.confirmed_start < utcnow() + timedelta(hours=26))):
        _schedule_reminders(db, appt)
        n += 1
    return {"checked": n}
