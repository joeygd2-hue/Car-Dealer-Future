"""Persistent outreach agent, end to end in sandbox: discovery → qualify → draft → approve → send →
reply monitoring → classification → stop sequence → meeting booking. Plus the stop/limit/compliance gates."""

import pytest
from sqlalchemy import func, select, update

from app import db as dbmod
from app.db import utcnow
from app.integrations.base import ProspectCandidate
from app.integrations.sandbox import add_simulated_inbound_email
from app.models import (
    Campaign,
    CostEntry,
    Enrollment,
    IntegrationConnection,
    Job,
    OutreachMessage,
    OutreachReply,
    Prospect,
    SalesMeeting,
    SandboxRecord,
    Suppression,
)
from app.services import jobs
from app.services.outreach import agent
from app.services.outreach.booking import available_slots, book
from app.services.outreach.classify import classify_reply
from app.services.outreach.compliance import add_suppression
from app.services.outreach.discovery import add_candidate
from tests.conftest import run_worker


def tick_all(db, rounds=6):
    for _ in range(rounds):
        db.execute(update(Job).where(Job.status == "QUEUED").values(run_at=utcnow()))
        db.commit()
        jobs.enqueue(db, "outreach.tick", {}, idempotency_key=f"t:{utcnow().timestamp()}")
        jobs.enqueue(db, "outreach.poll_inbox", {}, idempotency_key=f"p:{utcnow().timestamp()}")
        db.commit()
        run_worker()
    db.expire_all()


@pytest.fixture
def running(db, world):
    st = agent.get_state(db)
    st.send_window_start_hour, st.send_window_end_hour = 0, 24
    st.send_weekdays = [0, 1, 2, 3, 4, 5, 6]
    st.target_queue_size, st.max_discoveries_per_tick = 6, 6
    st.max_sends_per_hour, st.max_sends_per_day = 50, 50
    c = Campaign(name="Test", status="ACTIVE", content_approved_at=utcnow(), content_approved_by_id=world["platform"].id,
                 require_message_approval=True, daily_send_limit=50)
    db.add(c)
    agent.start(db, user_id=world["platform"].id)
    db.commit()
    return st


def test_full_sandbox_lifecycle(db, world, running):
    tick_all(db)
    assert db.scalar(select(func.count(Prospect.id))) >= 6
    assert db.scalar(select(func.count(Prospect.id)).where(Prospect.is_simulated.is_(False))) == 0
    statuses = {p.status for p in db.scalars(select(Prospect))}
    assert "ENROLLED" in statuses
    pending = list(db.scalars(select(OutreachMessage).where(OutreachMessage.status == "PENDING_APPROVAL")))
    assert pending, "drafts must wait for human approval"
    msg = pending[0]
    assert msg.subject.startswith("ADV:") and "unsubscribe" in msg.body.lower() and "/u/" in msg.body
    agent.approve_message(db, msg, user_id=world["platform"].id)
    db.commit()
    tick_all(db, 2)
    msg = db.get(OutreachMessage, msg.id)
    assert msg.status == "SIMULATED" and msg.rfc_message_id
    out = next(r for r in db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_OUT"))
               if r.payload["to"] == msg.to_address)
    assert out.payload["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    enr = db.get(Enrollment, msg.enrollment_id)
    assert enr.current_step == 1 and enr.next_action_at > utcnow()
    # Prospect replies positively → sequence stops, booking link proposed (needs approval: auto-respond off).
    add_simulated_inbound_email(db, from_addr=msg.to_address, subject="Re: hi", body="Sounds interesting, tell me more",
                                in_reply_to=msg.rfc_message_id)
    db.commit()
    tick_all(db, 2)
    reply = db.scalar(select(OutreachReply))
    assert reply.classification == "POSITIVE" and reply.outreach_message_id == msg.id
    assert db.get(Enrollment, enr.id).status == "STOPPED" and db.get(Enrollment, enr.id).stop_reason == "replied"
    prop = db.scalar(select(OutreachMessage).where(OutreachMessage.kind == "REPLY"))
    assert prop.status == "PENDING_APPROVAL" and "/book/" in prop.body
    # Duplicate inbox delivery is ignored.
    tick_all(db, 1)
    assert db.scalar(select(func.count(OutreachReply.id))) == 1
    # Prospect books a slot → calendar + video (sandbox) → CONFIRMED, prospect MEETING_BOOKED.
    meeting = db.scalar(select(SalesMeeting))
    slots = available_slots(db, running)
    assert slots
    book(db, meeting, slots[0], running)
    db.commit()
    assert meeting.status == "CONFIRMED" and meeting.simulated and meeting.video_join_url
    assert db.get(Prospect, msg.prospect_id).status == "MEETING_BOOKED"
    assert slots[0] not in available_slots(db, running)  # slot now busy
    with pytest.raises(ValueError):
        book(db, meeting, slots[1], running)  # link already used


def _prospect_with_draft(db, world, running):
    tick_all(db, 3)
    msg = db.scalar(select(OutreachMessage).where(OutreachMessage.status == "PENDING_APPROVAL"))
    return msg


def test_opt_out_enforced_before_send(db, world, running):
    msg = _prospect_with_draft(db, world, running)
    agent.approve_message(db, msg, user_id=world["platform"].id)
    add_suppression(db, "EMAIL", msg.to_address, "OPT_OUT", "test")
    db.commit()
    tick_all(db, 2)
    msg = db.get(OutreachMessage, msg.id)
    assert msg.status == "BLOCKED" and any("suppressed" in r for r in msg.block_reasons)
    assert not [r for r in db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_OUT"))
                if r.payload["to"] == msg.to_address]


def test_unsubscribe_link_and_reply_opt_out(db, world, running, client):
    msg = _prospect_with_draft(db, world, running)
    r = client.post(f"/u/{msg.unsubscribe_token}")
    assert r.status_code == 200
    db.expire_all()
    assert db.scalar(select(Suppression).where(Suppression.value == msg.to_address)) is not None
    assert db.get(Prospect, msg.prospect_id).status == "DO_NOT_CONTACT"
    assert db.get(OutreachMessage, msg.id).status == "CANCELLED"


def test_reply_opt_out_suppresses_domain(db, world, running):
    msg = _prospect_with_draft(db, world, running)
    agent.approve_message(db, msg, user_id=world["platform"].id)
    db.commit()
    tick_all(db, 2)
    add_simulated_inbound_email(db, from_addr=msg.to_address, subject="Re", body="Please remove me from your list",
                                in_reply_to=db.get(OutreachMessage, msg.id).rfc_message_id)
    db.commit()
    tick_all(db, 2)
    p = db.get(Prospect, msg.prospect_id)
    assert p.status == "DO_NOT_CONTACT"
    assert db.scalar(select(Suppression).where(Suppression.kind == "DOMAIN", Suppression.value == p.domain)) is not None


def test_rate_and_budget_limits_defer(db, world, running):
    msg = _prospect_with_draft(db, world, running)
    running.max_sends_per_hour = 0
    agent.approve_message(db, msg, user_id=world["platform"].id)
    db.commit()
    tick_all(db, 1)
    m = db.get(OutreachMessage, msg.id)
    assert m.status == "APPROVED" and any("outreach:sends:hour" in r for r in m.block_reasons)
    assert not [r for r in db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_OUT"))
                if r.payload["to"] == m.to_address]
    job = db.scalar(select(Job).where(Job.type == "outreach.send", Job.status == "QUEUED"))
    assert job is not None and job.attempts == 0  # deferred, not failed
    running.max_sends_per_hour = 50
    running.daily_budget_cents = 1
    db.add(CostEntry(scope="OUTREACH", provider="x", operation="y", amount_cents=5.0))
    db.commit()
    db.execute(update(Job).where(Job.status == "QUEUED").values(run_at=utcnow()))
    db.commit()
    run_worker()
    db.expire_all()
    assert "daily budget reached" in db.get(OutreachMessage, msg.id).block_reasons


def test_emergency_stop(db, world, running):
    msg = _prospect_with_draft(db, world, running)
    agent.approve_message(db, msg, user_id=world["platform"].id)
    jobs.enqueue(db, "outreach.send", {"message_id": msg.id}, idempotency_key="manual-send")
    db.commit()
    agent.emergency_stop(db, user_id=world["platform"].id, reason="test")
    db.commit()
    assert db.scalar(select(Job).where(Job.idempotency_key == "manual-send")).status == "CANCELLED"
    # Even a send job that slips through is held at send time (not sent, not destroyed).
    jobs.enqueue(db, "outreach.send", {"message_id": msg.id}, idempotency_key="slipped")
    db.commit()
    run_worker()
    db.expire_all()
    held = db.get(OutreachMessage, msg.id)
    assert held.status == "APPROVED" and any("emergency" in r for r in held.block_reasons)
    assert not [r for r in db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_OUT"))
                if r.payload["to"] == held.to_address]
    with pytest.raises(ValueError):
        agent.start(db, user_id=world["platform"].id)
    agent.reset_emergency(db, user_id=world["platform"].id, reason="verified")
    agent.start(db, user_id=world["platform"].id)
    assert agent.get_state(db).state == "RUNNING"


def test_duplicate_prevention(db, world, running):
    c = ProspectCandidate(name="Same Motors", source="CSV", source_ref=None, city="Sugar Land", website="https://same.example",
                          email="sales@same.example")
    p1, new1 = add_candidate(db, c)
    p2, new2 = add_candidate(db, ProspectCandidate(name="Same Motors LLC", source="SANDBOX", source_ref="x",
                                                   website="http://www.same.example/", city="Sugar Land"))
    assert new1 and not new2 and p1.id == p2.id
    msg = _prospect_with_draft(db, world, running)
    agent.approve_message(db, msg, user_id=world["platform"].id)
    db.commit()
    tick_all(db, 2)
    # A second copy of the same step to the same address is refused even with a fresh idempotency key.
    dup = OutreachMessage(enrollment_id=msg.enrollment_id, prospect_id=msg.prospect_id, campaign_id=msg.campaign_id,
                          step_index=msg.step_index, to_address=msg.to_address, subject=msg.subject, body=msg.body,
                          status="APPROVED", idempotency_key="dup-copy")
    enr = db.get(Enrollment, msg.enrollment_id)
    enr.status = "ACTIVE"
    db.add(dup)
    db.commit()
    jobs.enqueue(db, "outreach.send", {"message_id": dup.id}, idempotency_key="dup-send")
    db.commit()
    run_worker()
    db.expire_all()
    assert db.get(OutreachMessage, dup.id).status == "BLOCKED"


def test_integration_failure_retries_then_dead(db, world, running):
    db.add(IntegrationConnection(dealership_id=None, provider="OUTREACH_EMAIL", external_account_id="sandbox-smtp",
                                 config={"sandbox_failure_mode": "retryable"}))
    msg = _prospect_with_draft(db, world, running)
    agent.approve_message(db, msg, user_id=world["platform"].id)
    db.commit()
    for _ in range(running.max_task_retries + 2):
        tick_all(db, 1)
    m = db.get(OutreachMessage, msg.id)
    assert m.status == "FAILED" and "503" in (m.error or "")


def test_scheduler_recovery_after_restart(db, world, running):
    """State lives in the DB: a brand-new worker resumes the agent with no in-memory context."""
    from app.worker import Worker, install_recurring_tasks

    install_recurring_tasks()
    w1 = Worker(worker_id="before-restart", poll_seconds=0)
    w1.run_once()
    del w1
    w2 = Worker(worker_id="after-restart", poll_seconds=0)
    for _ in range(4):
        db.execute(update(Job).where(Job.status == "QUEUED").values(run_at=utcnow()))
        from app.models import RecurringTask
        db.execute(update(RecurringTask).values(next_run_at=utcnow()))
        db.commit()
        w2.run_once()
    db.expire_all()
    assert db.scalar(select(func.count(Prospect.id))) > 0
    assert agent.get_state(db).last_tick_at is not None


def test_live_mode_guards(db, world, running):
    from app.services.outreach.compliance import check_message

    msg = _prospect_with_draft(db, world, running)
    agent.approve_message(db, msg, user_id=world["platform"].id)
    res = check_message(db, msg, running, live=True)
    assert any("simulated prospect" in r for r in res.stop)
    assert any("not verified by a human" in r for r in res.stop)
    assert any("CAN-SPAM" in r for r in res.stop)


@pytest.mark.parametrize("subject,body,label", [
    ("Re: hi", "Sounds great, tell me more", "POSITIVE"),
    ("Re: hi", "Can we set up a call next Tuesday?", "MEETING_REQUEST"),
    ("Re: hi", "How much does it cost?", "QUESTION"),
    ("Re: hi", "Not interested, thanks.", "NOT_INTERESTED"),
    ("Re: hi", "Unsubscribe", "OPT_OUT"),
    ("Automatic reply: Out of office", "I'm away until Monday", "OUT_OF_OFFICE"),
    ("Undeliverable: hi", "Address not found", "BOUNCE"),
    ("Re: hi", "How did you get my email? This is spam.", "SENSITIVE"),
    ("Re: hola", "Me interesa, suena bien", "POSITIVE"),
    ("Re: hola", "No estamos interesados", "NOT_INTERESTED"),
    ("Re: hola", "Por favor no me envíen más correos", "OPT_OUT"),
    ("Re: hi", "ok", "UNKNOWN"),
])
def test_reply_classification(subject, body, label):
    assert classify_reply(subject, body).label == label


def test_classification_ignores_quoted_text():
    body = "No thanks.\n\nOn Mon, Oct 12, Sender wrote:\n> Would a call be useful? Let's set up a call"
    assert classify_reply("Re: hi", body).label == "NOT_INTERESTED"


def test_cold_sms_never_allowed(db, world, running):
    from app.services.outreach.compliance import check_message

    msg = _prospect_with_draft(db, world, running)
    msg.channel = "SMS"
    agent.approve_message(db, msg, user_id=world["platform"].id)
    assert any("not authorized" in r for r in check_message(db, msg, running, live=False).stop)
    _ = dbmod
