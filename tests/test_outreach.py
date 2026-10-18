"""Outreach agent tests: sandbox providers and simulated prospects only."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.jobs import enqueue
from app.core.time import utcnow
from app.models import (
    AgentRun, Campaign, CompanyFact, CostEntry, Enrollment, Escalation, Job, Meeting, OutreachMessage, PlatformIntegration, Prospect,
    ProspectContact, SandboxMail, Suppression,
)
from app.outreach import agent, classifier, policy
from app.outreach.providers.sandbox import inject_reply
from tests.conftest import login, run_worker

ALWAYS_OPEN = {"timezone": "America/Chicago", "days": [0, 1, 2, 3, 4, 5, 6], "start_hour": 0, "end_hour": 24}


@pytest.fixture
def outreach(session, world):
    email = PlatformIntegration(kind="email", provider="sandbox", name="Sandbox mailbox", config={"daily_cap": 100})
    cal = PlatformIntegration(kind="calendar", provider="sandbox", name="Sandbox calendar")
    disc = PlatformIntegration(kind="discovery", provider="sandbox", name="Sandbox discovery")
    session.add_all([email, cal, disc])
    session.add_all([
        CompanyFact(topic="company_overview", keywords=[], answer="We prepare accurate English and Spanish vehicle listings for independent dealers.",
                    approved_by=world["users"]["admin"].id),
        CompanyFact(topic="pricing", keywords=["price", "pricing", "cost", "how much"],
                    answer="Plans start at a fixed monthly fee; we share a written quote after a short call.", approved_by=world["users"]["admin"].id),
    ])
    session.flush()
    c = Campaign(name="Sugar Land pilot", status="active", sender_name="Joey at Agency", sender_email="joey@agency.test",
                 postal_address="100 Example Rd, Sugar Land, TX 77478", operating_schedule=ALWAYS_OPEN, daily_send_limit=20,
                 hourly_send_limit=20, max_followups=2, email_integration_id=email.id, calendar_integration_id=cal.id,
                 target_criteria={"min_queue": 5, "queries": ["used car dealer in Sugar Land, TX"]})
    session.add(c)
    session.commit()
    return {"email": email, "cal": cal, "disc": disc, "campaign": c}


def discover_and_enrich(n_rounds=6):
    for _ in range(n_rounds):
        enqueue_tick()
        run_worker(max_rounds=20)


def enqueue_tick():
    from app.db import SessionLocal
    s = SessionLocal()
    enqueue(s, "outreach.agent_tick", queue="outreach", idempotency_key=None)
    s.commit()
    s.close()


def approve_all(session, user_id):
    for m in session.scalars(select(OutreachMessage).where(OutreachMessage.status == "awaiting_approval")).all():
        agent.approve_message(session, m.id, user_id)
    session.commit()


def test_discovery_enrichment_and_qualification(session, outreach):
    discover_and_enrich(4)
    session.expire_all()
    prospects = session.scalars(select(Prospect)).all()
    assert len(prospects) == 12 and all(p.is_simulated for p in prospects)
    toyota = next(p for p in prospects if p.business_name == "Sugar Land Toyota")
    assert toyota.status == "disqualified" and "independent" in toyota.disqualify_reason
    good = next(p for p in prospects if p.business_name == "Brazos Valley Auto Sales")
    contact = session.scalar(select(ProspectContact).where(ProspectContact.prospect_id == good.id))
    assert contact.email == "sales@brazosvalleyautosales.example" and contact.evidence_url.endswith("/contact")
    assert good.status in ("enrolled", "contacted")
    # running discovery again creates no duplicates
    discover_and_enrich(2)
    assert session.scalar(select(func.count()).select_from(Prospect)) == 12


def test_first_touch_requires_approval_and_sends_compliant_email(session, outreach, world):
    discover_and_enrich(4)
    session.expire_all()
    drafts = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "awaiting_approval")).all()
    assert drafts, "agent should draft first touches"
    assert session.scalar(select(func.count()).select_from(SandboxMail)) == 0  # nothing sent without approval
    approve_all(session, world["users"]["staff"].id)
    discover_and_enrich(1)
    session.expire_all()
    sent = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "sent")).all()
    assert len(sent) == len(drafts)
    m = sent[0]
    assert "100 Example Rd, Sugar Land, TX 77478" in m.body and "/u/" in m.body and "unsubscribe" in m.body.lower()
    assert all(c["passed"] for c in m.policy_checks)
    assert not m.subject.lower().startswith("re:")
    assert session.scalar(select(func.count()).select_from(SandboxMail).where(SandboxMail.direction == "outbound")) == len(sent)


def _sent_one(session, outreach, world):
    discover_and_enrich(4)
    approve_all(session, world["users"]["staff"].id)
    discover_and_enrich(1)
    session.expire_all()
    return session.scalars(select(OutreachMessage).where(OutreachMessage.status == "sent").order_by(OutreachMessage.created_at)).first()


def test_opt_out_reply_suppresses_and_stops_sequence(session, outreach, world):
    m = _sent_one(session, outreach, world)
    inject_reply(session, thread_id=m.thread_id, from_address=m.to_address, to_address=m.from_address, subject="Re: hi",
                 body="Please remove me from your list.\n\nOn Mon, Joey wrote:\n> Would you be open to a call?")
    session.commit()
    discover_and_enrich(1)
    session.expire_all()
    assert policy.is_suppressed(session, m.to_address)
    p = session.get(Prospect, m.prospect_id)
    assert p.status == "do_not_contact"
    e = session.get(Enrollment, m.enrollment_id)
    assert e.state == "stopped"
    # follow-up time passes: nothing further is drafted or sent to this prospect
    e.next_action_at = utcnow() - timedelta(days=1)
    session.commit()
    discover_and_enrich(2)
    session.expire_all()
    later = session.scalars(select(OutreachMessage).where(OutreachMessage.prospect_id == p.id, OutreachMessage.direction == "outbound",
                                                          OutreachMessage.status == "sent")).all()
    assert len(later) == 1


def test_suppression_blocks_send_even_if_approved(session, outreach, world):
    discover_and_enrich(4)
    session.expire_all()
    m = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "awaiting_approval")).first()
    agent.approve_message(session, m.id, world["users"]["staff"].id)
    policy.suppress(session, "domain", m.to_address.split("@")[1], "manual_dnc", "test")
    session.commit()
    status = agent.send_message(session, m)
    assert status == "blocked" and "not_suppressed" in m.error


def test_unsubscribe_link(session, outreach, world, client):
    m = _sent_one(session, outreach, world)
    token = policy.unsubscribe_token(m.id)
    assert client.get(f"/u/{token}").status_code == 200
    assert client.post(f"/u/{token}").status_code == 200
    assert client.post("/u/forged.token").status_code == 400
    session.expire_all()
    assert policy.is_suppressed(session, m.to_address).reason == "opt_out"


def test_positive_reply_offers_slots_and_books_meeting(session, outreach, world):
    c = session.get(Campaign, outreach["campaign"].id)
    c.auto_reply_routine = True
    c.auto_book_meetings = True
    session.commit()
    m = _sent_one(session, outreach, world)
    inject_reply(session, thread_id=m.thread_id, from_address=m.to_address, to_address=m.from_address, subject=f"Re: {m.subject}",
                 body="Sounds interesting. When are you available for a call?")
    session.commit()
    discover_and_enrich(1)
    session.expire_all()
    mtg = session.scalar(select(Meeting).where(Meeting.prospect_id == m.prospect_id))
    assert mtg.status == "offered" and len(mtg.offered_slots) == 3
    offer = session.scalar(select(OutreachMessage).where(OutreachMessage.kind == "meeting_offer"))
    assert offer.status == "sent" and "Option 1" in offer.body
    assert session.get(Enrollment, m.enrollment_id).state == "replied"
    inject_reply(session, thread_id=m.thread_id, from_address=m.to_address, to_address=m.from_address, subject="Re: times",
                 body="Option 2 works for me.")
    session.commit()
    discover_and_enrich(1)
    session.expire_all()
    mtg = session.get(Meeting, mtg.id)
    assert mtg.status == "booked" and mtg.video_link.startswith("https://meet.sandbox.invalid/")
    assert mtg.start_at.isoformat() == mtg.offered_slots[1]["start"]
    assert session.get(Prospect, m.prospect_id).status == "meeting_booked"
    conf = session.scalar(select(OutreachMessage).where(OutreachMessage.kind == "meeting_confirmation"))
    assert conf.status == "sent"


def test_meeting_booking_needs_approval_by_default(session, outreach, world, client):
    m = _sent_one(session, outreach, world)
    inject_reply(session, thread_id=m.thread_id, from_address=m.to_address, to_address=m.from_address, subject="Re", body="Let's talk. Demo?")
    session.commit()
    discover_and_enrich(1)
    approve_all(session, world["users"]["staff"].id)  # approve the offer reply
    discover_and_enrich(1)
    inject_reply(session, thread_id=m.thread_id, from_address=m.to_address, to_address=m.from_address, subject="Re", body="the first one")
    session.commit()
    discover_and_enrich(1)
    session.expire_all()
    esc = session.scalar(select(Escalation).where(Escalation.kind == "meeting_approval"))
    assert esc is not None
    mtg = session.scalar(select(Meeting).where(Meeting.prospect_id == m.prospect_id))
    assert mtg.status == "offered"
    h = login(client, "staff@agency.test")
    r = client.post(f"/api/outreach/meetings/{mtg.id}/book", headers=h, json={"slot_index": 0})
    assert r.status_code == 200 and r.json()["status"] == "booked"


def test_question_answered_only_from_approved_facts(session, outreach, world):
    m = _sent_one(session, outreach, world)
    inject_reply(session, thread_id=m.thread_id, from_address=m.to_address, to_address=m.from_address, subject="Re", body="How much does it cost?")
    inject_reply(session, thread_id=m.thread_id, from_address=m.to_address, to_address=m.from_address, subject="Re",
                 body="Can you integrate with our accounting system?")
    session.commit()
    discover_and_enrich(1)
    session.expire_all()
    reply = session.scalar(select(OutreachMessage).where(OutreachMessage.kind == "reply", OutreachMessage.direction == "outbound"))
    assert "written quote" in reply.body and reply.status == "awaiting_approval"  # auto_reply_routine is off by default
    assert session.scalar(select(Escalation).where(Escalation.kind == "question")) is not None


@pytest.mark.parametrize("body,expected", [
    ("Unsubscribe", "opt_out"), ("STOP", "opt_out"), ("Por favor, no me contacte más", "opt_out"),
    ("I am out of the office until Monday", "out_of_office"), ("Not interested, thanks", "not_interested"),
    ("Can we schedule a call next week?", "meeting_request"), ("What does it cost?", "question"),
    ("This is spam, I will report you", "complaint"), ("You should talk to our manager Ana", "referral"),
    ("Sounds good, tell me more", "positive_interest"), ("ok", "other"),
    ("Interested, but please stop emailing me", "opt_out"),  # safest interpretation wins
])
def test_classifier(body, expected):
    assert classifier.classify(from_address="x@y.example", subject="Re: hello", body=body).category == expected


def test_bounce_classification():
    c = classifier.classify(from_address="MAILER-DAEMON@mail.example", subject="Undeliverable: hello", body="Address not found")
    assert c.category == "bounce"


def test_rate_limits_and_budget_defer_sends(session, outreach, world):
    c = session.get(Campaign, outreach["campaign"].id)
    c.hourly_send_limit = 2
    session.commit()
    discover_and_enrich(4)
    approve_all(session, world["users"]["staff"].id)
    discover_and_enrich(1)
    session.expire_all()
    sent = session.scalar(select(func.count()).select_from(OutreachMessage).where(OutreachMessage.status == "sent"))
    deferred = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "approved")).all()
    assert sent == 2 and deferred and all("campaign_hourly_limit" in m.error for m in deferred)
    # budget: an exhausted daily budget blocks further sends
    c.hourly_send_limit = 50
    c.daily_budget_usd = 0.5
    session.add(CostEntry(category="ai", provider="anthropic", amount_usd=0.75, measured=True, campaign_id=c.id))
    session.commit()
    m = deferred[0]
    m.send_after = None
    assert agent.send_message(session, m) == "approved" and "daily_budget" in m.error


def test_operating_schedule_defers_outside_hours(session, outreach, world):
    c = session.get(Campaign, outreach["campaign"].id)
    discover_and_enrich(4)
    c.operating_schedule = {"timezone": "America/Chicago", "days": [0, 1, 2, 3, 4], "start_hour": 9, "end_hour": 17}
    session.commit()
    m = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "awaiting_approval")).first()
    agent.approve_message(session, m.id, world["users"]["staff"].id)
    from tests.conftest import utc
    saturday_night = utc(2026, 10, 17, 4, 0)  # Fri 11pm Chicago
    agent.send_message(session, m, now=saturday_night)
    assert m.status == "approved" and m.send_after > saturday_night and "operating_schedule" in m.error


def test_duplicate_prevention(session, outreach, world):
    discover_and_enrich(4)
    session.expire_all()
    # one enrollment per prospect, one message per (enrollment, step)
    per_prospect = session.execute(select(Enrollment.prospect_id, func.count()).group_by(Enrollment.prospect_id)).all()
    assert all(n == 1 for _, n in per_prospect)
    e = session.scalars(select(Enrollment)).first()
    e.state = "needs_draft"
    session.commit()
    before = session.scalar(select(func.count()).select_from(OutreachMessage).where(OutreachMessage.enrollment_id == e.id))
    assert agent.draft_next(session, e) is None
    session.commit()
    assert session.scalar(select(func.count()).select_from(OutreachMessage).where(OutreachMessage.enrollment_id == e.id)) == before
    assert e.state == "awaiting_approval"
    # an interrupted send is never retried automatically
    m = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "awaiting_approval")).first()
    m.status, m.sent_at = "sending", utcnow() - timedelta(minutes=30)
    session.commit()
    assert agent.recover_interrupted_sends(session) == 1
    assert m.status == "failed"
    assert session.scalar(select(Escalation).where(Escalation.kind == "send_outcome_unknown")) is not None


def test_provider_failure_retries_then_escalates(session, outreach, world):
    discover_and_enrich(4)
    approve_all(session, world["users"]["staff"].id)
    integ = session.get(PlatformIntegration, outreach["email"].id)
    integ.config = {**integ.config, "fail_sends": True}
    session.commit()
    m = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "approved")).first()
    for i in range(3):
        m.send_after = None
        integ.health_status = "ok"
        session.commit()
        agent.send_message(session, m)
        session.commit()
    assert m.status == "failed" and m.attempts == 3
    assert session.scalar(select(Escalation).where(Escalation.kind == "send_failed")) is not None
    assert session.scalar(select(func.count()).select_from(SandboxMail)) == 0


def test_live_provider_blocked_without_authorization(session, outreach, world):
    discover_and_enrich(4)
    session.expire_all()
    live = PlatformIntegration(kind="email", provider="gmail", name="Agency Gmail", config={})
    session.add(live)
    session.flush()
    m = session.scalars(select(OutreachMessage).where(OutreachMessage.status == "awaiting_approval")).first()
    agent.approve_message(session, m.id, world["users"]["staff"].id)
    m.integration_id = live.id
    session.commit()
    assert agent.send_message(session, m) == "blocked"
    failed = set(policy.failed(m.policy_checks))
    assert {"live_allowed_env", "integration_live_authorized", "campaign_live_authorized", "not_simulated_prospect"} <= failed


def test_emergency_stop_and_pause(session, outreach, world, client):
    discover_and_enrich(4)
    approve_all(session, world["users"]["staff"].id)
    h = login(client, "staff@agency.test")
    r = client.post("/api/outreach/emergency-stop", headers=h, json={"reason": "test stop"})
    assert r.status_code == 200 and r.json()["held_messages"] > 0
    discover_and_enrich(1)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(SandboxMail).where(SandboxMail.direction == "outbound")) == 0
    run = session.scalar(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(1))
    assert run.state == "stopped"
    assert client.post("/api/outreach/emergency-stop/clear", headers=h).status_code == 403  # admin only
    ha = login(client, "admin@agency.test")
    assert client.post("/api/outreach/emergency-stop/clear", headers=ha).status_code == 200
    assert client.post("/api/outreach/pause", headers=h, json={"paused": True}).status_code == 200
    discover_and_enrich(1)
    session.expire_all()
    assert session.scalar(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(1)).state == "skipped_paused"


def test_agent_tick_recovers_after_worker_crash(session, outreach):
    from app.core import jobs
    job, _ = enqueue(session, "outreach.agent_tick", queue="outreach")
    session.commit()
    jobs.claim_next(session, "crashed-worker", lease_seconds=30)  # claimed, never finished
    later = utcnow() + timedelta(minutes=2)
    run_worker(now=later)
    run_worker(now=later + timedelta(hours=2))
    session.expire_all()
    assert session.get(Job, job.id).status == "succeeded"


def test_dashboard_reports_measured_vs_estimated(session, outreach, world, client):
    _sent_one(session, outreach, world)
    session.add(CostEntry(category="places", provider="google_places", amount_usd=0.035, measured=False))
    session.add(CostEntry(category="ai", provider="anthropic", amount_usd=0.01, measured=True))
    session.commit()
    h = login(client, "staff@agency.test")
    d = client.get("/api/outreach/dashboard", headers=h).json()
    assert d["measured"]["outreach_attempts_sent"] >= 1
    assert d["measured"]["sent_to_simulated_prospects"] == d["measured"]["outreach_attempts_sent"]
    assert d["costs"]["estimated_usd"] == 0.035 and d["costs"]["measured_usd"] == 0.01
    assert d["agent"]["state"] in ("active", "idle", "degraded")
