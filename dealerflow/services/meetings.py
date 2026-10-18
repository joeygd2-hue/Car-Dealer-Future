"""Meeting scheduling: request -> real availability -> approval -> Zoom + calendar -> booked.

A meeting is only ``booked`` after both providers return success, and the provider responses are
stored. Suggested times come only from a live free/busy query; if it fails, no times are shown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit, track_integration
from ..config import get_settings
from ..db import utcnow
from ..integrations.calendar import get_calendar_provider
from ..integrations.zoom import get_zoom_provider
from ..jobs import enqueue, handler, on_failure
from ..models import Meeting, Prospect, User
from ..security import role_at_least
from .crm import add_activity, advance_stage, create_task

ACTIVE = ("requested", "pending_approval", "booked", "rescheduled")


class MeetingError(ValueError):
    pass


def open_request(db: Session, p: Prospect, *, attendee_email: str | None, source: str,
                 actor_id: int | None = None) -> Meeting:
    existing = db.scalar(select(Meeting).where(Meeting.prospect_id == p.id, Meeting.status.in_(ACTIVE)))
    if existing:
        return existing
    m = Meeting(prospect_id=p.id, status="requested", attendee_email=attendee_email,
                topic=f"{get_settings().business_name or 'Intro'} × {p.name.replace('(SIMULATED)', '').strip()}"[:255],
                requested_by_id=actor_id, simulated=p.simulated)
    db.add(m)
    db.flush()
    add_activity(db, p, "meeting", f"Meeting requested ({source})", actor_id=actor_id, ref_type="meeting", ref_id=m.id)
    create_task(db, f"Offer meeting times to {p.name}", utcnow() + timedelta(hours=4), prospect=p,
                kind="meeting", dedupe_key=f"meeting_request:{m.id}")
    return m


def _local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(get_settings().timezone))


def _to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def suggest_slots(db: Session, m: Meeting, *, days: int = 7, max_slots: int = 6,
                  now: datetime | None = None) -> list[dict]:
    """Query the real calendar and return free slots within working hours. Raises on provider failure."""
    s = get_settings()
    cal = get_calendar_provider()
    now = now or utcnow()
    start = now + timedelta(hours=2)
    end = now + timedelta(days=days)
    with track_integration(db, cal.name, "freebusy", simulated=cal.simulated) as ctx:
        busy = cal.busy(start, end)
        ctx["detail"] = f"{len(busy)} busy block(s)"
    length = timedelta(minutes=s.meeting_length_minutes)
    buf = timedelta(minutes=s.meeting_buffer_minutes)
    tz = ZoneInfo(s.timezone)
    slots: list[dict] = []
    day = _local(start).replace(hour=0, minute=0, second=0, microsecond=0)
    while day < _local(end) and len(slots) < max_slots:
        if day.weekday() in s.working_day_list:
            t = day.replace(hour=s.working_hours_start, tzinfo=tz)
            while t + length <= day.replace(hour=s.working_hours_end, tzinfo=tz):
                su, eu = _to_utc(t), _to_utc(t + length)
                if su >= start and not any(b.start < eu + buf and b.end > su - buf for b in busy):
                    slots.append({"start": su.isoformat(), "end": eu.isoformat(),
                                  "label": t.strftime("%a %b %d, %I:%M %p %Z")})
                    t += timedelta(minutes=90)  # spread suggestions out
                    if len(slots) >= max_slots:
                        break
                    continue
                t += timedelta(minutes=30)
        day += timedelta(days=1)
    m.proposed_slots = slots
    m.availability_checked_at = utcnow()
    m.availability_source = cal.name + (" (SIMULATED)" if cal.simulated else "")
    return slots


def choose_slot(db: Session, m: Meeting, start_iso: str, *, actor: User | None) -> Meeting:
    """Record the time the prospect agreed to. Needs approval before anything is created."""
    if m.status not in ("requested", "pending_approval"):
        raise MeetingError(f"Meeting is {m.status}")
    start = datetime.fromisoformat(start_iso)
    if not m.proposed_slots or start_iso not in {sl["start"] for sl in m.proposed_slots}:
        raise MeetingError("Time must be one of the slots from the last availability check")
    m.start_at = start
    m.end_at = start + timedelta(minutes=get_settings().meeting_length_minutes)
    m.status = "pending_approval"
    add_activity(db, m.prospect, "meeting", f"Time chosen: {_local(start):%a %b %d %I:%M %p} — awaiting approval",
                 actor_id=actor.id if actor else None, ref_type="meeting", ref_id=m.id)
    return m


def approve_booking(db: Session, m: Meeting, *, actor: User) -> None:
    if not role_at_least(actor.role, "manager"):
        raise MeetingError("Only a manager or admin can approve a booking")
    if m.status != "pending_approval" or not m.start_at:
        raise MeetingError("Choose a time first")
    if m.start_at < utcnow():
        raise MeetingError("That time has passed; check availability again")
    p = m.prospect
    if p.do_not_contact:
        raise MeetingError("Prospect is do-not-contact")
    m.approved_by_id = actor.id
    audit(db, "meeting.approve", actor_id=actor.id, entity_type="meeting", entity_id=m.id,
          data={"start": m.start_at.isoformat()})
    enqueue(db, "book_meeting", f"book_meeting:{m.id}:{m.start_at.isoformat()}", {"meeting_id": m.id}, max_attempts=3)


@handler("book_meeting")
def book_meeting_job(db: Session, payload: dict) -> dict:
    m = db.get(Meeting, payload["meeting_id"])
    if m is None or m.status in ("booked", "rescheduled", "cancelled", "completed"):
        return {"skipped": m.status if m else "missing"}
    s = get_settings()
    cal, zoom = get_calendar_provider(), get_zoom_provider()
    # Re-check availability right before booking: the slot may have been taken since it was offered.
    with track_integration(db, cal.name, "freebusy_recheck", simulated=cal.simulated):
        busy = cal.busy(m.start_at - timedelta(minutes=1), m.end_at + timedelta(minutes=1))
    if busy and not m.zoom_meeting_id:
        m.status, m.error = "requested", "Slot no longer free at booking time; offer new times"
        add_activity(db, m.prospect, "meeting", "Booking aborted: slot became busy", ref_type="meeting", ref_id=m.id)
        return {"aborted": "slot busy"}
    responses = dict(m.provider_response or {})
    if not m.zoom_meeting_id:  # idempotent: a retry after a calendar failure reuses the Zoom meeting
        attempt = responses.get("zoom_attempt")
        zm = None
        if attempt and attempt.get("start") == m.start_at.isoformat():
            # A previous attempt may have created the meeting and lost the response: look before creating.
            with track_integration(db, zoom.name, "find_meeting", simulated=zoom.simulated) as ctx:
                zm = zoom.find_meeting(topic=m.topic, start=m.start_at)
                ctx["detail"] = f"recovered {zm.meeting_id}" if zm else "none found"
        if zm is None:
            responses["zoom_attempt"] = {"start": m.start_at.isoformat(), "at": utcnow().isoformat()}
            m.provider_response = dict(responses)
            db.commit()  # checkpoint the attempt before the non-idempotent create
            with track_integration(db, zoom.name, "create_meeting", simulated=zoom.simulated) as ctx:
                zm = zoom.create_meeting(topic=m.topic, start=m.start_at, duration_minutes=s.meeting_length_minutes,
                                         timezone_name=s.timezone, agenda=f"Intro call with {m.prospect.name}")
                ctx["detail"] = f"meeting {zm.meeting_id}"
        m.zoom_meeting_id, m.zoom_join_url = zm.meeting_id, zm.join_url
        responses["zoom"] = zm.raw
        m.provider_response = responses
        db.commit()  # checkpoint
    if not m.calendar_event_id:
        with track_integration(db, cal.name, "create_event", simulated=cal.simulated) as ctx:
            ev = cal.create_event(start=m.start_at, end=m.end_at, summary=m.topic,
                                  description=f"Zoom: {m.zoom_join_url}", attendee_email=m.attendee_email,
                                  location=m.zoom_join_url, request_id=f"meeting-{m.id}-{m.start_at.isoformat()}")
            ctx["detail"] = f"event {ev.event_id}"
        m.calendar_event_id = ev.event_id
        responses["calendar"] = {"id": ev.event_id, "htmlLink": ev.html_link, "simulated": ev.simulated}
        m.provider_response = responses
    m.simulated = m.simulated or cal.simulated or zoom.simulated
    m.status, m.error = "booked", None
    tag = " [SIMULATED]" if m.simulated else ""
    p = m.prospect
    add_activity(db, p, "meeting", f"Meeting booked{tag}: {_local(m.start_at):%a %b %d %I:%M %p}",
                 body=f"Zoom {m.zoom_meeting_id}\nCalendar event {m.calendar_event_id}", ref_type="meeting", ref_id=m.id)
    advance_stage(db, p, "meeting_booked", reason=f"Meeting #{m.id} confirmed by providers{tag}")
    from .sequences import stop_enrollments
    stop_enrollments(db, p, "meeting_booked")
    create_task(db, f"Prepare for meeting with {p.name}", m.start_at - timedelta(days=1), prospect=p,
                kind="meeting_prep", dedupe_key=f"meeting_prep:{m.id}:{m.start_at.isoformat()}")
    audit(db, "meeting.booked", entity_type="meeting", entity_id=m.id, data={"simulated": m.simulated})
    return {"booked": m.id, "simulated": m.simulated}


@on_failure("book_meeting")
def _book_failed(db: Session, payload: dict, error: str) -> None:
    m = db.get(Meeting, payload["meeting_id"])
    if m and m.status == "pending_approval":
        m.status, m.error = "failed", error
        add_activity(db, m.prospect, "meeting", "Booking failed", body=error, ref_type="meeting", ref_id=m.id)


def reschedule(db: Session, m: Meeting, new_start_iso: str, *, actor: User) -> None:
    if m.status not in ("booked", "rescheduled"):
        raise MeetingError("Only a booked meeting can be rescheduled")
    if not m.proposed_slots or new_start_iso not in {sl["start"] for sl in m.proposed_slots}:
        raise MeetingError("Run an availability check and pick one of its slots")
    s = get_settings()
    start = datetime.fromisoformat(new_start_iso)
    end = start + timedelta(minutes=s.meeting_length_minutes)
    cal, zoom = get_calendar_provider(), get_zoom_provider()
    with track_integration(db, zoom.name, "update_meeting", simulated=zoom.simulated):
        zoom.update_meeting(m.zoom_meeting_id, start=start, duration_minutes=s.meeting_length_minutes)
    with track_integration(db, cal.name, "update_event", simulated=cal.simulated):
        cal.update_event(m.calendar_event_id, start=start, end=end)
    old = m.start_at
    m.start_at, m.end_at, m.status = start, end, "rescheduled"
    add_activity(db, m.prospect, "meeting", f"Rescheduled from {_local(old):%b %d %I:%M %p} to {_local(start):%b %d %I:%M %p}",
                 actor_id=actor.id, ref_type="meeting", ref_id=m.id)
    create_task(db, f"Prepare for meeting with {m.prospect.name}", start - timedelta(days=1), prospect=m.prospect,
                kind="meeting_prep", dedupe_key=f"meeting_prep:{m.id}:{start.isoformat()}")
    audit(db, "meeting.reschedule", actor_id=actor.id, entity_type="meeting", entity_id=m.id)


def cancel(db: Session, m: Meeting, *, actor: User, reason: str) -> None:
    if m.status in ("cancelled", "completed"):
        return
    cal, zoom = get_calendar_provider(), get_zoom_provider()
    if m.zoom_meeting_id:
        with track_integration(db, zoom.name, "delete_meeting", simulated=zoom.simulated):
            zoom.delete_meeting(m.zoom_meeting_id)
    if m.calendar_event_id:
        with track_integration(db, cal.name, "delete_event", simulated=cal.simulated):
            cal.delete_event(m.calendar_event_id)
    m.status = "cancelled"
    add_activity(db, m.prospect, "meeting", "Meeting cancelled", body=reason, actor_id=actor.id, ref_type="meeting", ref_id=m.id)
    audit(db, "meeting.cancel", actor_id=actor.id, entity_type="meeting", entity_id=m.id, data={"reason": reason})


def complete(db: Session, m: Meeting, *, actor: User | None, outcome: str = "completed", notes: str = "") -> None:
    if m.status not in ("booked", "rescheduled"):
        raise MeetingError("Only a booked meeting can be completed")
    m.status = "no_show" if outcome == "no_show" else "completed"
    add_activity(db, m.prospect, "meeting", f"Meeting {m.status.replace('_', ' ')}", body=notes,
                 actor_id=actor.id if actor else None, ref_type="meeting", ref_id=m.id)
    create_task(db, f"Next step after meeting with {m.prospect.name}", utcnow() + timedelta(days=1),
                prospect=m.prospect, kind="follow_up", dedupe_key=f"after_meeting:{m.id}")
    audit(db, "meeting.complete", actor_id=actor.id if actor else None, entity_type="meeting", entity_id=m.id,
          data={"outcome": m.status})
