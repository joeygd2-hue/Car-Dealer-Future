"""Controlled live verification of real integrations (run by the owner; never automatic).

Sends only to addresses listed in DF_TEST_INBOXES, creates and then deletes one Zoom meeting and
one calendar event far in the future, and reports each step's real provider response. Each step
is recorded in the audit and integration logs. Nothing here contacts a prospect.

  python -m dealerflow live-check send --to you@yourdomain.com --i-control-this-inbox
  (reply to that email from the inbox, then click its unsubscribe link)
  python -m dealerflow live-check inbox      # fetch replies, show matching + classification
  python -m dealerflow live-check meeting    # freebusy + create/delete Zoom meeting and event
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from .audit import audit, track_integration
from .config import get_settings
from .db import session_scope, utcnow
from .integrations.calendar import get_calendar_provider
from .integrations.zoom import get_zoom_provider
from .jobs import run_job
from .models import InboundMessage, Job, OutreachMessage, Prospect, Suppression, Template
from .services.outreach import create_draft
from .services.prospects import LeadRecord, intake

NAME = "LIVE CHECK — owner's controlled inbox"


def send(to: str, *, confirmed: bool) -> dict:
    s = get_settings()
    if not confirmed:
        raise SystemExit("Add --i-control-this-inbox to confirm the address is yours")
    if to.lower() not in s.test_inbox_list:
        raise SystemExit(f"{to} is not in DF_TEST_INBOXES; add it there first")
    if s.email_provider == "mock":
        raise SystemExit("DF_EMAIL_PROVIDER is mock; configure the real provider first")
    with session_scope() as db:
        # Test record: counted with simulated/test data, never as a business result.
        p = intake(db, LeadRecord(name=NAME, source="live_check", email=to, dealership_type="other", simulated=True,
                                  notes="Created by live-check. Not a prospect.")).prospect
        tpl = db.scalar(select(Template).where(Template.name == "intro", Template.language == "en"))
        msg = create_draft(db, p, tpl, actor=None, origin="live_check")
        msg.subject = f"[dealerflow live check] {msg.subject}"
        msg.simulated = True  # a controlled test, not outreach
        msg.status, msg.approval_kind, msg.queued_at = "queued", "live_check", utcnow()
        db.add(Job(kind="send_email", key=f"send_email:{msg.id}", payload={"message_id": msg.id}, max_attempts=1))
        audit(db, "livecheck.send", data={"to": to, "message_id": msg.id})
        mid = msg.id
    with session_scope() as db:
        job_id = db.scalar(select(Job.id).where(Job.key == f"send_email:{mid}"))
    status = run_job(job_id)
    with session_scope() as db:
        m = db.get(OutreachMessage, mid)
        return {"job": status, "message_status": m.status, "provider": m.provider,
                "provider_message_id": m.provider_message_id, "error": m.error,
                "next": "Reply to it from that inbox, click the unsubscribe link, then run: live-check inbox"}


def inbox() -> dict:
    from .services.inbox import poll_inbox_job

    with session_scope() as db:
        res = poll_inbox_job(db, {})
        p = db.scalar(select(Prospect).where(Prospect.name == NAME))
        rows = list(db.scalars(select(InboundMessage).where(InboundMessage.prospect_id == p.id))) if p else []
        sup = db.scalar(select(Suppression).where(Suppression.value == (p.email if p else "")))
        return {"poll": res, "matched_replies": [(r.subject, r.classification, r.matched_by) for r in rows],
                "suppressed": bool(sup), "suppression_reason": sup.reason if sup else None,
                "stage": p.stage if p else None}


def meeting() -> dict:
    s = get_settings()
    cal, zoom = get_calendar_provider(), get_zoom_provider()
    if cal.simulated or zoom.simulated:
        raise SystemExit("Calendar or Zoom provider is mock; configure the real providers first")
    start = (utcnow() + timedelta(days=30)).replace(hour=15, minute=0, second=0, microsecond=0)
    out: dict = {}
    with session_scope() as db:
        with track_integration(db, cal.name, "livecheck_freebusy"):
            out["busy_blocks_next_7_days"] = len(cal.busy(utcnow(), utcnow() + timedelta(days=7)))
        with track_integration(db, zoom.name, "livecheck_create_meeting"):
            zm = zoom.create_meeting(topic="dealerflow live check (auto-deleted)", start=start, duration_minutes=15,
                                     timezone_name=s.timezone, agenda="Created and deleted by live-check")
        out["zoom_meeting_id"] = zm.meeting_id
        try:
            with track_integration(db, cal.name, "livecheck_create_event"):
                ev = cal.create_event(start=start, end=start + timedelta(minutes=15),
                                      summary="dealerflow live check (auto-deleted)", description=zm.join_url,
                                      attendee_email=None, location=zm.join_url, request_id=f"livecheck-{start.isoformat()}")
            out["calendar_event_id"] = ev.event_id
            with track_integration(db, cal.name, "livecheck_delete_event"):
                cal.delete_event(ev.event_id)
        finally:
            with track_integration(db, zoom.name, "livecheck_delete_meeting"):
                zoom.delete_meeting(zm.meeting_id)
        out["cleaned_up"] = True
        audit(db, "livecheck.meeting", data=out)
    return out
