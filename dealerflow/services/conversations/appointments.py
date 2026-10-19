"""Customer test-drive appointments. Never presented to the customer as confirmed until a staff member confirms
it. Separate from Phase 1's sales ``meetings``. The dealership calendar is a sandbox in this build."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...integrations.errors import IntegrationError
from ...jobs import enqueue, handler
from ...models import Appointment, AppointmentEvent, Client, Conversation, Vehicle
from . import notifications
from .channels import SENDABLE_CHANNELS, CalendarError, dealership_calendar
from .common import local_to_utc, record, utc_to_local
from .responder.engine import describe_when
from .responder.facts import full_address, vehicle_title

ACTIVE = ("REQUESTED", "PENDING_CONFIRMATION", "CONFIRMED", "RESCHEDULE_REQUESTED")


def _event(db: Session, appt: Appointment, event: str, actor: str, **details) -> None:
    db.add(AppointmentEvent(client_id=appt.client_id, appointment_id=appt.id, event=event, actor=actor, details=details))


def check_availability(db: Session, appt: Appointment) -> str:
    cal = dealership_calendar(db, appt.client_id)
    if cal is None:
        appt.availability_checked = "NO_CALENDAR"
    else:
        start = appt.requested_start
        try:
            busy = [b for b in cal.free_busy(start, start + timedelta(minutes=appt.duration_minutes))]
            appt.availability_checked = "BUSY" if busy else "FREE"
            appt.calendar_provider = cal.name
        except (CalendarError, IntegrationError) as exc:  # includes the emergency stop
            appt.availability_checked = "CHECK_FAILED"
            _event(db, appt, "AVAILABILITY_CHECK_FAILED", "system", error=str(exc))
            return appt.availability_checked
    _event(db, appt, "AVAILABILITY_CHECKED", "system", result=appt.availability_checked)
    return appt.availability_checked


def request_from_conversation(db: Session, conv: Conversation, start_local: datetime, *, name: str | None,
                              phone: str | None, email: str | None) -> Appointment:
    client = db.get(Client, conv.client_id)
    start = local_to_utc(start_local, client.timezone)
    existing = db.scalar(select(Appointment).where(Appointment.conversation_id == conv.id,
                                                   Appointment.status.in_(ACTIVE)))
    if existing and existing.requested_start == start:
        return existing
    if existing and existing.status != "CONFIRMED":
        existing.status = "CANCELLED"
        _event(db, existing, "SUPERSEDED", "customer", new_start=start.isoformat())
    appt = Appointment(client_id=conv.client_id, conversation_id=conv.id, contact_id=conv.contact_id,
                       vehicle_id=conv.vehicle_id, requested_start=start, customer_name=name, customer_phone=phone,
                       customer_email=email, status="REQUESTED")
    db.add(appt)
    db.flush()
    _event(db, appt, "REQUESTED", "customer", start_local=start_local.isoformat())
    check_availability(db, appt)
    appt.status = "PENDING_CONFIRMATION"
    conv.appointment_status = "PENDING_CONFIRMATION"
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    record(db, "appointment.requested", client_id=conv.client_id, entity_type="appointment", entity_id=appt.id,
           start=start.isoformat(), availability=appt.availability_checked)
    notifications.notify(
        db, conv.client_id, kind="APPOINTMENT",
        subject=f"Test drive request: {describe_when(start_local, 'en')}",
        body=(f"Customer {name or '(name not provided)'} requested a test drive"
              f"{' of the ' + vehicle_title(vehicle) if vehicle else ''} for {describe_when(start_local, 'en')}.\n"
              f"Calendar check: {appt.availability_checked}. Please confirm or decline."),
        link=f"/dealers/{conv.client_id}/appointments", assigned_user_id=conv.assigned_user_id,
        dedupe_key=f"appt-req:{appt.id}")
    return appt


def confirm(db: Session, appt: Appointment, *, user_id: int, start_utc: datetime | None = None) -> Appointment:
    if appt.status not in ("REQUESTED", "PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED"):
        raise ValueError(f"cannot confirm appointment in status {appt.status}")
    client = db.get(Client, appt.client_id)
    start = start_utc or appt.requested_start
    cal = dealership_calendar(db, appt.client_id)
    if cal is not None:
        vehicle = db.get(Vehicle, appt.vehicle_id) if appt.vehicle_id else None
        # Raises CalendarError/IntegrationError → caller shows the error; appointment stays unconfirmed.
        appt.calendar_event_id = cal.create_event(
            summary=f"Test drive: {appt.customer_name or 'customer'}" + (f" — {vehicle_title(vehicle)}" if vehicle else ""),
            start=start, end=start + timedelta(minutes=appt.duration_minutes),
            description=f"Booked via conversation #{appt.conversation_id}", location=full_address(client))
        appt.calendar_provider = cal.name
    appt.status = "CONFIRMED"
    appt.confirmed_start = start
    appt.assigned_user_id = appt.assigned_user_id or user_id
    _event(db, appt, "CONFIRMED", f"user:{user_id}", start=start.isoformat(), calendar_event_id=appt.calendar_event_id)
    record(db, "appointment.confirmed", client_id=appt.client_id, actor_id=user_id, entity_type="appointment",
           entity_id=appt.id, start=start.isoformat(), calendar_event_id=appt.calendar_event_id)
    _customer_message(db, appt, "confirmation")
    _schedule_reminders(db, appt)
    _sync_conv(db, appt)
    return appt


def decline(db: Session, appt: Appointment, *, user_id: int, note: str | None = None) -> None:
    appt.status = "DECLINED"
    appt.notes = note
    _event(db, appt, "DECLINED", f"user:{user_id}", note=note)
    record(db, "appointment.declined", client_id=appt.client_id, actor_id=user_id, entity_type="appointment",
           entity_id=appt.id)
    _sync_conv(db, appt)


def cancel(db: Session, appt: Appointment, *, actor: str, user_id: int | None = None, reason: str | None = None,
           notify_customer: bool = True) -> None:
    if appt.calendar_event_id:
        cal = dealership_calendar(db, appt.client_id)
        if cal is not None:
            try:
                cal.cancel_event(appt.calendar_event_id)
            except (CalendarError, IntegrationError) as exc:
                _event(db, appt, "CALENDAR_CANCEL_FAILED", actor, error=str(exc))
    was_confirmed = appt.status == "CONFIRMED"
    appt.status = "CANCELLED"
    _event(db, appt, "CANCELLED", actor, reason=reason)
    record(db, "appointment.cancelled", client_id=appt.client_id, actor_id=user_id, entity_type="appointment",
           entity_id=appt.id, reason=reason)
    if notify_customer and was_confirmed:
        _customer_message(db, appt, "cancellation")
    _sync_conv(db, appt)


def reschedule(db: Session, appt: Appointment, new_start_utc: datetime, *, user_id: int) -> Appointment:
    if appt.calendar_event_id:
        cal = dealership_calendar(db, appt.client_id)
        if cal is not None:
            try:
                cal.cancel_event(appt.calendar_event_id)
            except (CalendarError, IntegrationError) as exc:
                _event(db, appt, "CALENDAR_CANCEL_FAILED", f"user:{user_id}", error=str(exc))
        appt.calendar_event_id = None
    old = appt.confirmed_start or appt.requested_start
    appt.requested_start = new_start_utc
    appt.confirmed_start = None
    appt.status = "RESCHEDULE_REQUESTED"
    _event(db, appt, "RESCHEDULED", f"user:{user_id}", old=old.isoformat(), new=new_start_utc.isoformat())
    check_availability(db, appt)
    record(db, "appointment.rescheduled", client_id=appt.client_id, actor_id=user_id, entity_type="appointment",
           entity_id=appt.id)
    _sync_conv(db, appt)
    return appt


def _sync_conv(db: Session, appt: Appointment) -> None:
    if appt.conversation_id:
        conv = db.get(Conversation, appt.conversation_id)
        if conv:
            conv.appointment_status = appt.status
            from .summaries import build_summary

            build_summary(db, conv)


CUSTOMER_TEXT = {
    "confirmation": {"en": "Your test drive at {dealer} is confirmed for {when}. Reply here if you need to make a change.",
                     "es": "Su prueba de manejo en {dealer} está confirmada para el {when}. Responda aquí si necesita hacer algún cambio."},
    "reminder": {"en": "Reminder: your test drive at {dealer} is scheduled for {when}.",
                 "es": "Recordatorio: su prueba de manejo en {dealer} está programada para el {when}."},
    "cancellation": {"en": "Your test drive at {dealer} for {when} has been cancelled. Reply here if you'd like a new time.",
                     "es": "Su prueba de manejo en {dealer} para el {when} fue cancelada. Responda aquí si desea otra hora."},
}


def _customer_message(db: Session, appt: Appointment, kind: str, label: str | None = None) -> None:
    if not appt.conversation_id:
        return
    conv = db.get(Conversation, appt.conversation_id)
    if conv is None or conv.opted_out or conv.channel not in SENDABLE_CHANNELS:
        _event(db, appt, f"{kind.upper()}_NOT_SENT", "system",
               reason="opted out" if conv and conv.opted_out else "channel requires manual staff message")
        return
    client = db.get(Client, appt.client_id)
    start = appt.confirmed_start or appt.requested_start
    when = describe_when(utc_to_local(start, client.timezone), conv.preferred_language)
    body = CUSTOMER_TEXT[kind][conv.preferred_language].format(dealer=client.name, when=when)
    from .outbound import queue_outbound

    # One key per message: the 24h and 2h reminders must not collide (Phase 3 used the same key for both).
    tag = f"{kind}-{label}" if label else kind
    msg = queue_outbound(db, conv, body, sender_type="SYSTEM", idempotency_key=f"appt:{appt.id}:{tag}:{start.isoformat()}")
    _event(db, appt, f"{kind.upper()}_QUEUED", "system", message_id=msg.id)


def _schedule_reminders(db: Session, appt: Appointment) -> None:
    start = appt.confirmed_start
    for label, delta in (("24h", timedelta(hours=24)), ("2h", timedelta(hours=2))):
        run_at = start - delta
        if run_at > utcnow():
            enqueue(db, "conversations.appointment_reminder", f"reminder:{appt.id}:{start.isoformat()}:{label}",
                    {"appointment_id": appt.id, "start": start.isoformat(), "label": label}, delay=run_at - utcnow())


@handler("conversations.appointment_reminder")
def reminder_job(db: Session, payload: dict) -> dict:
    appt = db.get(Appointment, int(payload["appointment_id"]))
    if appt is None or appt.status != "CONFIRMED" or not appt.confirmed_start \
            or appt.confirmed_start.isoformat() != payload.get("start"):
        return {"skipped": "appointment changed"}
    if appt.confirmed_start < utcnow():
        return {"skipped": "in the past"}
    _customer_message(db, appt, "reminder", payload.get("label"))
    return {"queued": True}


@handler("conversations.dispatch_reminders")
def dispatch_reminders_job(db: Session, payload: dict) -> dict:
    """Recovery sweep: make sure every upcoming confirmed appointment has reminder jobs."""
    horizon = utcnow() + timedelta(hours=26)
    n = 0
    for appt in db.scalars(select(Appointment).where(Appointment.status == "CONFIRMED",
                                                     Appointment.confirmed_start > utcnow(),
                                                     Appointment.confirmed_start < horizon)):
        _schedule_reminders(db, appt)
        n += 1
    return {"checked": n}


def customer_change_requested(db: Session, conv: Conversation, text: str) -> Appointment | None:
    """Customer asked to cancel/reschedule. Staff decide; we only flag it (never auto-cancel or auto-move)."""
    appt = db.scalar(select(Appointment).where(Appointment.conversation_id == conv.id, Appointment.status.in_(ACTIVE))
                     .order_by(Appointment.id.desc()))
    if appt is None:
        return None
    appt.status = "RESCHEDULE_REQUESTED"
    _event(db, appt, "CUSTOMER_CHANGE_REQUESTED", "customer", message=text[:500])
    record(db, "appointment.customer_change_requested", client_id=appt.client_id, entity_type="appointment",
           entity_id=appt.id)
    _sync_conv(db, appt)
    return appt
