"""Customer test-drive appointments. Never presented to the customer as confirmed until a staff
member (or a connected calendar via staff action) confirms it. Separate from SalesMeeting."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import audit
from ...db import utcnow
from ...integrations.customer_channels import SENDABLE_CHANNELS
from ...integrations.errors import IntegrationError
from ...jobs import enqueue, handler
from ...models_customer import CustomerConversation, TestDriveAppointment, TestDriveAppointmentEvent
from ...models_dealer import Client, Vehicle
from ..responder.engine import describe_when
from ..responder.facts import client_address, vehicle_title
from . import notify as notifications


def dealership_calendar(db: Session, client_id: int):
    """Per-dealership test-drive calendar. None: no dealership calendar integration exists in this build, so
    availability is reported as NO_CALENDAR and a staff member confirms every request by hand."""
    return None

ACTIVE = ("REQUESTED", "PENDING_CONFIRMATION", "CONFIRMED", "RESCHEDULE_REQUESTED")


def local_to_utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def utc_to_local(dt: datetime, tz: str) -> datetime:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def _event(db: Session, appt: TestDriveAppointment, event: str, actor: str, **details) -> None:
    db.add(TestDriveAppointmentEvent(client_id=appt.client_id, appointment_id=appt.id, event=event, actor=actor, details=details))


def check_availability(db: Session, appt: TestDriveAppointment) -> str:
    cal = dealership_calendar(db, appt.client_id)
    if cal is None:
        appt.availability_checked = "NO_CALENDAR"
    else:
        start = appt.requested_start
        try:
            busy = cal.free_busy(start, start + timedelta(minutes=appt.duration_minutes))
            appt.availability_checked = "BUSY" if busy else "FREE"
            appt.calendar_provider = cal.name
        except IntegrationError as exc:
            appt.availability_checked = "CHECK_FAILED"
            _event(db, appt, "AVAILABILITY_CHECK_FAILED", "system", error=str(exc))
            return appt.availability_checked
    _event(db, appt, "AVAILABILITY_CHECKED", "system", result=appt.availability_checked)
    return appt.availability_checked


def request_from_conversation(db: Session, conv: CustomerConversation, start_local: datetime, *, name: str | None,
                              phone: str | None, email: str | None) -> TestDriveAppointment:
    dealership = db.get(Client, conv.client_id)
    start = local_to_utc(start_local, dealership.timezone)
    existing = db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == conv.id,
                                                   TestDriveAppointment.status.in_(ACTIVE)))
    if existing and existing.requested_start == start:
        return existing
    if existing and existing.status != "CONFIRMED":
        existing.status = "CANCELLED"
        _event(db, existing, "SUPERSEDED", "customer", new_start=start.isoformat())
    appt = TestDriveAppointment(client_id=conv.client_id, conversation_id=conv.id, contact_id=conv.contact_id,
                       vehicle_id=conv.vehicle_id, requested_start=start, customer_name=name, customer_phone=phone,
                       customer_email=email, status="REQUESTED")
    db.add(appt)
    db.flush()
    _event(db, appt, "REQUESTED", "customer", start_local=start_local.isoformat())
    check_availability(db, appt)
    appt.status = "PENDING_CONFIRMATION"
    conv.appointment_status = "PENDING_CONFIRMATION"
    conv.lead_qualified = True
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    audit.event(db, "appointment.requested", client_id=conv.client_id,
                 entity_type="appointment", entity_id=appt.id, start=start.isoformat(),
                 availability=appt.availability_checked)
    notifications.notify(
        db, conv.client_id, kind="APPOINTMENT",
        subject=f"Test drive request: {describe_when(start_local, 'en')}",
        body=(f"Customer {name or '(name not provided)'} requested a test drive"
              f"{' of the ' + vehicle_title(vehicle) if vehicle else ''} for {describe_when(start_local, 'en')}.\n"
              f"Calendar check: {appt.availability_checked}. Please confirm or decline."),
        link=f"/dealers/{conv.client_id}/appointments", assigned_user_id=conv.assigned_user_id,
        dedupe_key=f"appt-req:{appt.id}")
    return appt


def confirm(db: Session, appt: TestDriveAppointment, *, user_id: int, start_utc: datetime | None = None) -> TestDriveAppointment:
    if appt.status not in ("REQUESTED", "PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED"):
        raise ValueError(f"cannot confirm appointment in status {appt.status}")
    dealership = db.get(Client, appt.client_id)
    start = start_utc or appt.requested_start
    cal = dealership_calendar(db, appt.client_id)
    if cal is not None:
        vehicle = db.get(Vehicle, appt.vehicle_id) if appt.vehicle_id else None
        # Raises IntegrationError → caller shows the error; appointment stays unconfirmed.
        appt.calendar_event_id = cal.create_event(
            summary=f"Test drive: {appt.customer_name or 'customer'}" + (f" — {vehicle_title(vehicle)}" if vehicle else ""),
            start=start, end=start + timedelta(minutes=appt.duration_minutes),
            description=f"Booked via conversation #{appt.conversation_id}", attendees=[],
            location=client_address(dealership))
        appt.calendar_provider = cal.name
    appt.status = "CONFIRMED"
    appt.confirmed_start = start
    appt.assigned_user_id = appt.assigned_user_id or user_id
    _event(db, appt, "CONFIRMED", f"user:{user_id}", start=start.isoformat(), calendar_event_id=appt.calendar_event_id)
    audit.event(db, "appointment.confirmed", actor_id=user_id, client_id=appt.client_id,
                 entity_type="appointment", entity_id=appt.id, start=start.isoformat(),
                 calendar_event_id=appt.calendar_event_id)
    _customer_message(db, appt, "confirmation")
    _schedule_reminders(db, appt)
    _sync_conv(db, appt)
    return appt


def decline(db: Session, appt: TestDriveAppointment, *, user_id: int, note: str | None = None) -> None:
    appt.status = "DECLINED"
    appt.notes = note
    _event(db, appt, "DECLINED", f"user:{user_id}", note=note)
    audit.event(db, "appointment.declined", actor_id=user_id, client_id=appt.client_id,
                 entity_type="appointment", entity_id=appt.id)
    _sync_conv(db, appt)


def cancel(db: Session, appt: TestDriveAppointment, *, actor: str, user_id: int | None = None, reason: str | None = None,
           notify_customer: bool = True) -> None:
    if appt.calendar_event_id:
        cal = dealership_calendar(db, appt.client_id)
        if cal is not None:
            try:
                cal.cancel_event(appt.calendar_event_id)
            except IntegrationError as exc:
                _event(db, appt, "CALENDAR_CANCEL_FAILED", actor, error=str(exc))
    was_confirmed = appt.status == "CONFIRMED"
    appt.status = "CANCELLED"
    _event(db, appt, "CANCELLED", actor, reason=reason)
    audit.event(db, "appointment.cancelled", actor_id=user_id,
                 client_id=appt.client_id, entity_type="appointment", entity_id=appt.id, reason=reason)
    if notify_customer and was_confirmed:
        _customer_message(db, appt, "cancellation")
    _sync_conv(db, appt)


def reschedule(db: Session, appt: TestDriveAppointment, new_start_utc: datetime, *, user_id: int) -> TestDriveAppointment:
    if appt.calendar_event_id:
        cal = dealership_calendar(db, appt.client_id)
        if cal is not None:
            try:
                cal.cancel_event(appt.calendar_event_id)
            except IntegrationError as exc:
                _event(db, appt, "CALENDAR_CANCEL_FAILED", f"user:{user_id}", error=str(exc))
        appt.calendar_event_id = None
    old = appt.confirmed_start or appt.requested_start
    appt.requested_start = new_start_utc
    appt.confirmed_start = None
    appt.status = "RESCHEDULE_REQUESTED"
    _event(db, appt, "RESCHEDULED", f"user:{user_id}", old=old.isoformat(), new=new_start_utc.isoformat())
    check_availability(db, appt)
    audit.event(db, "appointment.rescheduled", actor_id=user_id,
                 client_id=appt.client_id, entity_type="appointment", entity_id=appt.id)
    _sync_conv(db, appt)
    return appt


def _sync_conv(db: Session, appt: TestDriveAppointment) -> None:
    if appt.conversation_id:
        conv = db.get(CustomerConversation, appt.conversation_id)
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


def _customer_message(db: Session, appt: TestDriveAppointment, kind: str) -> None:
    if not appt.conversation_id:
        return
    conv = db.get(CustomerConversation, appt.conversation_id)
    if conv is None or conv.opted_out or conv.channel not in SENDABLE_CHANNELS:
        _event(db, appt, f"{kind.upper()}_NOT_SENT", "system",
               reason="opted out" if conv and conv.opted_out else "channel requires manual staff message")
        return
    dealership = db.get(Client, appt.client_id)
    start = appt.confirmed_start or appt.requested_start
    when = describe_when(utc_to_local(start, dealership.timezone), conv.preferred_language)
    body = CUSTOMER_TEXT[kind][conv.preferred_language].format(dealer=dealership.name, when=when)
    from .outbound import queue_outbound

    msg = queue_outbound(db, conv, body, sender_type="SYSTEM",
                         idempotency_key=f"appt:{appt.id}:{kind}:{start.isoformat()}")
    _event(db, appt, f"{kind.upper()}_QUEUED", "system", message_id=msg.id)


def _schedule_reminders(db: Session, appt: TestDriveAppointment) -> None:
    start = appt.confirmed_start
    for label, delta in (("24h", timedelta(hours=24)), ("2h", timedelta(hours=2))):
        run_at = start - delta
        if run_at > utcnow():
            enqueue(db, "customer.appt_reminder", f"customer.appt_reminder:{appt.id}:{start.isoformat()}:{label}",
                    {"appointment_id": appt.id, "start": start.isoformat(), "label": label}, delay=run_at - utcnow())


@handler("customer.appt_reminder")
def reminder_job(db: Session, payload: dict) -> dict:
    appt = db.get(TestDriveAppointment, payload["appointment_id"])
    if appt is None or appt.status != "CONFIRMED" or not appt.confirmed_start \
            or appt.confirmed_start.isoformat() != payload.get("start"):
        return {"skipped": "appointment changed"}
    if appt.confirmed_start < utcnow():
        return {"skipped": "in the past"}
    _customer_message(db, appt, "reminder")
    return {"queued": True}


@handler("customer.appt_reminder_sweep")
def dispatch_reminders_job(db: Session, payload: dict) -> dict:
    """Recovery sweep: make sure every upcoming confirmed appointment has reminder jobs."""
    horizon = utcnow() + timedelta(hours=26)
    n = 0
    for appt in db.scalars(select(TestDriveAppointment).where(TestDriveAppointment.status == "CONFIRMED",
                                                     TestDriveAppointment.confirmed_start > utcnow(),
                                                     TestDriveAppointment.confirmed_start < horizon)):
        _schedule_reminders(db, appt)
        n += 1
    return {"checked": n}


def customer_change_requested(db: Session, conv: CustomerConversation, text: str) -> TestDriveAppointment | None:
    """Customer asked to cancel/reschedule. Staff decide; we only flag it (never auto-cancel or auto-move)."""
    appt = db.scalar(select(TestDriveAppointment).where(TestDriveAppointment.conversation_id == conv.id, TestDriveAppointment.status.in_(ACTIVE))
                     .order_by(TestDriveAppointment.id.desc()))
    if appt is None:
        return None
    appt.status = "RESCHEDULE_REQUESTED"
    _event(db, appt, "CUSTOMER_CHANGE_REQUESTED", "customer", message=text[:500])
    audit.event(db, "appointment.customer_change_requested", client_id=appt.client_id,
                 entity_type="appointment", entity_id=appt.id)
    _sync_conv(db, appt)
    return appt
