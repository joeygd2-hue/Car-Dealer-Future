"""Autonomous agent: control states, emergency stop, budgets, rate limits, auto-approval rules, recovery."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.integrations.email import MockEmailProvider
from dealerflow.jobs import run_pending
from dealerflow.models import AgentRun, Campaign, Enrollment, Job, KnowledgeItem, OutreachMessage, Prospect, Task
from dealerflow.services import agent, outreach
from dealerflow.services.agent import ControlError, set_campaign_mode, set_status, tick


from dealerflow.worker import schedule_tick
from tests.conftest import make_user

# A Tuesday 15:00 UTC = 10:00 Central: inside default operating hours
IN_HOURS = datetime(2026, 10, 20, 15, 0)
NIGHT = datetime(2026, 10, 21, 7, 0)  # 02:00 Central


def _start(db, admin, *, auto=False, auto_reply=False):
    camp = db.scalar(select(Campaign))
    set_status(db, "running", actor=admin)
    set_campaign_mode(db, camp, actor=admin, status="active", approval_mode="auto" if auto else "manual",
                      auto_reply=auto_reply)
    db.commit()
    return camp


def test_stopped_agent_does_nothing(db, admin):
    run = tick(db, now=IN_HOURS)
    assert run.status == "skipped" and "stopped" in run.skipped_reason
    assert db.scalar(select(Prospect)) is None


def test_tick_replenishes_scores_enrolls_and_drafts_for_approval(db, admin):
    camp = _start(db, admin)
    run = tick(db, now=IN_HOURS)
    db.commit()
    assert run.status == "ok", run.errors
    assert run.counts["replenish"]["created"] == 7  # fixture discovery (SIMULATED)
    assert run.counts["score"]["scored"] == 7
    enrolled = list(db.scalars(select(Enrollment)))
    assert enrolled and all(e.campaign_id == camp.id for e in enrolled)
    drafts = list(db.scalars(select(OutreachMessage)))
    assert drafts and all(m.status == "draft" and m.origin == "agent" for m in drafts)  # manual approval mode
    assert MockEmailProvider.outbox == []
    # Franchise dealer must never be enrolled
    franchise = db.scalar(select(Prospect).where(Prospect.dealership_type == "franchise"))
    assert franchise.id not in {e.prospect_id for e in enrolled}
    # Second tick: no duplicate drafts while the first ones await approval
    tick(db, now=IN_HOURS + timedelta(minutes=5))
    db.commit()
    assert len(list(db.scalars(select(OutreachMessage)))) == len(drafts)


def test_auto_mode_sends_in_simulation_and_sequence_progresses(db, admin, monkeypatch):
    monkeypatch.setattr(agent, "in_operating_window", lambda cfg, now: True)
    _start(db, admin, auto=True)
    tick(db, now=IN_HOURS)
    db.commit()
    queued = list(db.scalars(select(OutreachMessage).where(OutreachMessage.status == "queued")))
    assert queued and all(m.approval_kind == "campaign_auto" for m in queued)
    run_pending()
    assert len(MockEmailProvider.outbox) == len(queued)
    # Step 2 is not drafted before the delay
    tick(db, now=utcnow() + timedelta(days=1))
    db.commit()
    assert db.scalar(select(OutreachMessage).where(OutreachMessage.enrollment_step == 1)) is None


def test_auto_mode_needs_admin(db):
    mgr = make_user(db, "mgr@test.example", "manager")
    camp = db.scalar(select(Campaign))
    with pytest.raises(ControlError):
        set_campaign_mode(db, camp, actor=mgr, approval_mode="auto")


def test_real_provider_never_auto_sends_without_live_authorization(db, admin, monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.integrations import email as email_mod

    _start(db, admin, auto=True)

    class FakeReal(email_mod.MockEmailProvider):
        name, simulated = "fake_real", False

    monkeypatch.setattr("dealerflow.services.agent.get_email_provider", lambda: FakeReal())
    monkeypatch.setattr("dealerflow.services.outreach.get_email_provider", lambda: FakeReal())
    tick(db, now=IN_HOURS)
    db.commit()
    msgs = list(db.scalars(select(OutreachMessage)))
    assert msgs and all(m.status == "draft" for m in msgs)  # live not authorized -> waits for a person
    # A person approves one: the send job still defers until live is authorized AND env flag is on.
    outreach.approve(db, msgs[0], actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    job = db.scalar(select(Job).where(Job.kind == "send_email"))
    assert job.status == "pending" and "DF_OUTREACH_SENDING_ENABLED" in job.last_error
    assert FakeReal.outbox == []
    monkeypatch.setenv("DF_OUTREACH_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    job.run_after = utcnow()
    db.commit()
    run_pending()
    db.expire_all()
    job = db.scalar(select(Job).where(Job.kind == "send_email"))
    assert "not been authorized" in job.last_error and FakeReal.outbox == []


def test_emergency_stop_halts_sends_and_returns_queue_to_draft(db, admin):
    _start(db, admin, auto=True)
    tick(db, now=IN_HOURS)
    db.commit()
    assert db.scalar(select(OutreachMessage).where(OutreachMessage.status == "queued"))
    set_status(db, "emergency_stopped", actor=admin, reason="test")
    db.commit()
    run_pending()
    assert MockEmailProvider.outbox == []
    db.expire_all()
    assert {m.status for m in db.scalars(select(OutreachMessage))} == {"draft"}
    run = tick(db, now=IN_HOURS + timedelta(minutes=5))
    assert run.status == "skipped"
    # Only an admin may clear an emergency stop
    mgr = make_user(db, "mgr2@test.example", "manager")
    with pytest.raises(ControlError):
        set_status(db, "running", actor=mgr)


def test_pause_keeps_reading_opt_outs_only(db, admin):
    _start(db, admin)
    tick(db, now=IN_HOURS)
    db.commit()
    set_status(db, "paused", actor=admin)
    p = db.scalar(select(Prospect).where(Prospect.email.is_not(None)))
    MockEmailProvider.inject_reply(p.email, "stop", "unsubscribe")
    n_msgs = len(list(db.scalars(select(OutreachMessage))))
    run = tick(db, now=IN_HOURS + timedelta(minutes=10))
    db.commit()
    assert run.counts["sequences"] == {"skipped": "paused"} and run.counts["replenish"] == {"skipped": "paused"}
    assert run.counts["poll_inbox"]["stored"] == 1
    assert db.get(Prospect, p.id).do_not_contact
    assert len(list(db.scalars(select(OutreachMessage)))) == n_msgs


def test_operating_hours_block_drafting(db, admin):
    _start(db, admin)
    run = tick(db, now=NIGHT)
    assert run.counts["sequences"] == {"skipped": "outside operating hours"}


def test_budget_blocks_paid_discovery(db, admin):
    _start(db, admin)
    from dealerflow.services.settings_store import put_setting

    put_setting(db, "cost_estimates", {"discovery:fixture": 0.50}, actor_id=admin.id)
    agent.update_config(db, {"daily_budget_usd": 0.25}, actor=admin)
    run = tick(db, now=IN_HOURS)
    assert "budget" in run.counts["replenish"]["skipped"]
    agent.update_config(db, {"daily_budget_usd": 1.0}, actor=admin)
    run = tick(db, now=IN_HOURS + timedelta(minutes=5))
    assert run.counts["replenish"]["created"] == 7
    from dealerflow.services.costs import spent_today
    assert spent_today(db) == 0.5  # recorded as an estimate


def test_google_places_cannot_be_stored(db, admin):
    from dealerflow.services.discovery import run_source

    with pytest.raises(ValueError, match="place IDs"):
        run_source(db, "google_places")
    with pytest.raises(ControlError):
        agent.update_config(db, {"discovery_source": "google_places"}, actor=admin)


def test_discovery_limited_per_day(db, admin):
    _start(db, admin)
    agent.update_config(db, {"replenish_below": 1000}, actor=admin)
    tick(db, now=IN_HOURS)
    run = tick(db, now=IN_HOURS + timedelta(minutes=5))
    assert run.counts["replenish"] == {"skipped": "daily discovery limit reached"}


def test_campaign_daily_cap(db, admin, monkeypatch):
    camp = _start(db, admin)
    camp.daily_cap = 1
    db.commit()
    monkeypatch.setattr(agent, "in_operating_window", lambda cfg, now: True)
    run = tick(db, now=utcnow())  # message created_at uses the real clock
    assert run.counts["sequences"]["drafted"] == 1 and run.counts["sequences"]["capped"] >= 1
    assert len(list(db.scalars(select(OutreachMessage)))) == 1


def test_step_failure_does_not_stop_other_steps(db, admin, monkeypatch):
    _start(db, admin)
    MockEmailProvider.fail_next = [RuntimeError("inbox exploded")]
    run = tick(db, now=IN_HOURS)
    db.commit()
    assert run.status == "partial" and run.errors[0]["step"] == "poll_inbox"
    assert run.counts["score"]["scored"] == 7


def test_auto_answer_uses_only_approved_info_and_escalates_sensitive(db, admin, monkeypatch):
    monkeypatch.setattr(agent, "in_operating_window", lambda cfg, now: True)
    _start(db, admin, auto=True, auto_reply=True)
    kb = db.scalar(select(KnowledgeItem).where(KnowledgeItem.topic == "Pricing"))
    tick(db, now=IN_HOURS)
    db.commit()
    run_pending()
    sent = list(db.scalars(select(OutreachMessage).where(OutreachMessage.status == "sent")))
    a, b = sent[0], sent[1]
    MockEmailProvider.inject_reply(a.to_email, "Re: x", "How much does it cost per month?", in_reply_to=a.rfc_message_id)
    MockEmailProvider.inject_reply(b.to_email, "Re: x", "How much? Can you give us a discount on the contract?",
                                   in_reply_to=b.rfc_message_id)
    # Unapproved knowledge -> escalate
    run = tick(db, now=IN_HOURS + timedelta(minutes=10))
    db.commit()
    assert run.counts["handle_replies"]["escalated"] == 2
    assert db.scalar(select(Task).where(Task.kind == "review"))

    # Approve the pricing answer, new question -> agent drafts from approved text and (auto mode) queues it
    kb.answer_en, kb.approved = "Plans start at $X/month (TEST).", True
    db.commit()
    MockEmailProvider.inject_reply(a.to_email, "Re: x", "What's the price?", in_reply_to=a.rfc_message_id)
    run = tick(db, now=IN_HOURS + timedelta(minutes=20))
    db.commit()
    assert run.counts["handle_replies"]["drafted"] == 1
    reply = db.scalar(select(OutreachMessage).where(OutreachMessage.in_reply_to_inbound_id.is_not(None)))
    assert "Plans start at $X/month (TEST)." in reply.body and reply.origin == "agent"


def test_scheduler_ticks_are_idempotent_per_bucket(db):
    now = datetime(2026, 10, 20, 15, 2)
    assert schedule_tick(now) is True
    assert schedule_tick(now + timedelta(minutes=1)) is False  # same 5-minute bucket
    assert schedule_tick(now + timedelta(minutes=4)) is True
    assert len(list(db.scalars(select(Job).where(Job.kind == "agent_tick")))) == 2


def test_recovery_marks_interrupted_runs(db, admin):
    db.add(AgentRun(started_at=utcnow() - timedelta(hours=1), status="running"))
    db.commit()
    assert agent.recover_after_restart(db) == 1


def test_health_reports_worker_liveness(db, client):
    from dealerflow.worker import run_once

    assert client.get("/healthz").json()["worker_alive"] is False
    run_once("test-worker")
    h = client.get("/healthz").json()
    assert h["ok"] and h["worker_alive"] is True and h["agent_status"] == "stopped"


