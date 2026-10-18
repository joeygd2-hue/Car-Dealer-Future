"""Sales-meeting booking for prospects via the operator's calendar + video provider.

The prospect picks a slot on /book/{token}; the slot is re-checked against the calendar, the video
meeting and calendar event are created, and only then is the meeting CONFIRMED.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.integrations import registry
from app.integrations.base import IntegrationError
from app.models import Enrollment, OutreachAgentState, Prospect, SalesMeeting
from app.services import audit, notifications


def get_or_create_meeting(db: Session, prospect: Prospect) -> SalesMeeting:
    m = db.scalar(select(SalesMeeting).where(SalesMeeting.prospect_id == prospect.id, SalesMeeting.status == "PROPOSED"))
    if m is None:
        m = SalesMeeting(prospect_id=prospect.id, booking_token=secrets.token_urlsafe(16), simulated=prospect.is_simulated)
        db.add(m)
        db.flush()
    return m


def available_slots(db: Session, state: OutreachAgentState, days: int = 7, duration: int = 30,
                    now: datetime | None = None) -> list[datetime]:
    """UTC slot starts inside the operating window, excluding calendar busy times."""
    now = now or utcnow()
    tz = ZoneInfo(state.timezone)
    cal = registry.calendar(db)
    local_now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    start_range, end_range = now + timedelta(hours=12), now + timedelta(days=days + 1)
    busy = cal.free_busy(start_range, end_range)
    slots = []
    for d in range(1, days + 1):
        day = (local_now + timedelta(days=d)).date()
        if day.weekday() not in (state.send_weekdays or []):
            continue
        for hour in range(state.send_window_start_hour, state.send_window_end_hour):
            for minute in (0, 30):
                local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                start = local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
                end = start + timedelta(minutes=duration)
                if local.hour * 60 + minute + duration > state.send_window_end_hour * 60 or start < start_range:
                    continue
                if any(b.start < end and b.end > start for b in busy):
                    continue
                slots.append(start)
    return slots


def book(db: Session, meeting: SalesMeeting, start: datetime, state: OutreachAgentState) -> SalesMeeting:
    if meeting.status != "PROPOSED":
        raise ValueError("This booking link has already been used or cancelled.")
    prospect = db.get(Prospect, meeting.prospect_id)
    cal = registry.calendar(db)
    video = registry.video(db)
    end = start + timedelta(minutes=meeting.duration_minutes)
    try:
        if cal.free_busy(start, end):
            raise ValueError("That time is no longer available. Please pick another time.")
        vm = video.create_meeting(topic=f"Intro call — {prospect.name}", start=start, duration_minutes=meeting.duration_minutes)
        event_id = cal.create_event(summary=f"Intro call — {prospect.name}", start=start, end=end,
                                    description=f"Video meeting: {vm.join_url}\nProspect #{prospect.id}",
                                    attendees=[prospect.email] if prospect.email and not prospect.is_simulated else [])
    except IntegrationError as exc:
        meeting.error = str(exc)
        audit.record(db, "outreach.meeting_booking_failed", actor_type="AGENT", entity_type="sales_meeting",
                     entity_id=meeting.id, outcome="FAILED", error=str(exc))
        raise ValueError("We couldn't complete the booking right now. Please try again shortly.") from exc
    meeting.status = "CONFIRMED"
    meeting.start_at = start
    meeting.video_provider = video.name
    meeting.video_meeting_id = vm.meeting_id
    meeting.video_join_url = vm.join_url
    meeting.calendar_event_id = event_id
    meeting.simulated = cal.simulated or video.simulated
    prospect.status = "MEETING_BOOKED"
    for enr in db.scalars(select(Enrollment).where(Enrollment.prospect_id == prospect.id, Enrollment.status == "ACTIVE")):
        enr.status, enr.stop_reason = "STOPPED", "meeting_booked"
    audit.record(db, "outreach.meeting_booked", actor_type="AGENT", entity_type="sales_meeting", entity_id=meeting.id,
                 outcome="SIMULATED" if meeting.simulated else "OK", external=not meeting.simulated,
                 start=start.isoformat(), prospect_id=prospect.id)
    notifications.notify_operator(db, kind="APPOINTMENT", subject=f"Sales meeting booked: {prospect.name}",
                                  body=f"{prospect.name} booked {start:%Y-%m-%d %H:%M} UTC. Join: {vm.join_url}"
                                       + (" (SIMULATED — sandbox calendar/video)" if meeting.simulated else ""),
                                  link="/admin/outreach", dedupe_key=f"meeting:{meeting.id}")
    return meeting
