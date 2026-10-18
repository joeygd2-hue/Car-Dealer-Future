"""Step 9 scenario, end to end with SIMULATED providers:

import -> dedupe -> score -> review/approve -> send (mock) -> reply received and matched ->
CRM updated -> follow-up task -> meeting scheduled through the test calendar + Zoom.
"""

from datetime import timedelta

from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.integrations.calendar import BusyBlock, MockCalendarProvider
from dealerflow.integrations.email import MockEmailProvider
from dealerflow.integrations.zoom import MockZoomProvider
from dealerflow.jobs import run_pending
from dealerflow.models import InboundMessage, Meeting, OutreachMessage, Prospect, Task, Template
from dealerflow.services import meetings, outreach
from dealerflow.services.discovery import run_source
from dealerflow.services.inbox import poll_inbox_job
from dealerflow.services.scoring import score_prospect


def test_full_workflow(db, admin):
    # 1. Import from an approved (fixture) source; the deliberate duplicate merges.
    res = run_source(db, "fixture", actor_id=admin.id)
    assert res == {"found": 8, "created": 7, "merged": 1, "skipped": 0, "simulated": True}
    db.commit()
    p = db.scalar(select(Prospect).where(Prospect.name == "Brazos Bend Motors (SIMULATED)"))
    assert {s.external_id for s in p.sources} == {"fx-001", "fx-006"}
    assert p.simulated

    # 2. Score: explainable, uses fixture evidence.
    b = score_prospect(db, p)
    assert b["total"] >= 55 and b["eligible"]
    assert {f["key"] for f in b["factors"]} >= {"target_market_fit", "geographic_fit", "bilingual_opportunity"}

    # 3. Draft from template using verified fields only, then human approval.
    tpl = db.scalar(select(Template).where(Template.name == "intro", Template.language == "en"))
    msg = outreach.create_draft(db, p, tpl, actor=admin)
    assert msg.status == "draft" and "unsubscribe" in msg.body.lower() and "123 Test St" in msg.body
    assert "{" not in msg.subject
    outreach.approve(db, msg, actor=admin)
    db.commit()
    assert msg.status == "queued"
    assert MockEmailProvider.outbox == []  # nothing goes out at approval time

    # 4. Worker sends through the mock provider.
    counts = run_pending()
    assert counts.get("done") == 1
    db.expire_all()
    msg = db.get(OutreachMessage, msg.id)
    assert msg.status == "sent" and msg.simulated and msg.provider == "mock"
    assert len(MockEmailProvider.outbox) == 1
    out, _ = MockEmailProvider.outbox[0]
    assert out.unsubscribe_url and "/u/" in out.unsubscribe_url
    assert db.get(Prospect, p.id).stage == "contacted"

    # 5. A reply arrives and is matched to the right prospect via In-Reply-To.
    MockEmailProvider.inject_reply("sales@brazosbendmotors.example", "Re: " + msg.subject,
                                   "Sounds interesting. Can we set up a Zoom call next week?",
                                   in_reply_to=msg.rfc_message_id)
    r = poll_inbox_job(db, {})
    db.commit()
    assert r == {"fetched": 1, "stored": 1}
    inbound = db.scalar(select(InboundMessage))
    assert inbound.prospect_id == p.id and inbound.matched_by == "in_reply_to"
    assert inbound.classification == "meeting_request"
    p = db.get(Prospect, p.id)
    assert p.stage == "meeting_requested"
    assert db.get(OutreachMessage, msg.id).status == "replied"
    tasks = list(db.scalars(select(Task).where(Task.prospect_id == p.id, Task.status == "open")))
    assert any("Respond to" in t.title for t in tasks)
    assert any("Offer meeting times" in t.title for t in tasks)

    # 6. Meeting: real (mock) availability, approval, Zoom + calendar, booked only after both confirm.
    m = db.scalar(select(Meeting).where(Meeting.prospect_id == p.id))
    assert m.status == "requested"
    now = utcnow()
    MockCalendarProvider.busy_blocks = [BusyBlock(now, now + timedelta(days=2))]
    slots = meetings.suggest_slots(db, m)
    assert slots and all(s["start"] >= (now + timedelta(days=2)).isoformat()[:13] for s in slots)
    meetings.choose_slot(db, m, slots[0]["start"], actor=admin)
    assert m.status == "pending_approval" and not MockZoomProvider.meetings
    meetings.approve_booking(db, m, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    m = db.get(Meeting, m.id)
    assert m.status == "booked" and m.zoom_meeting_id and m.calendar_event_id
    assert m.provider_response["zoom"]["join_url"].startswith("https://simulated.invalid")
    assert db.get(Prospect, p.id).stage == "meeting_booked"
    assert db.scalar(select(Task).where(Task.prospect_id == p.id, Task.kind == "meeting_prep"))

    # 7. Reschedule and cancel go through the providers.
    slots = meetings.suggest_slots(db, m)
    meetings.reschedule(db, m, slots[-1]["start"], actor=admin)
    assert m.status == "rescheduled"
    meetings.cancel(db, m, actor=admin, reason="test")
    assert m.status == "cancelled" and m.zoom_meeting_id not in MockZoomProvider.meetings
