"""Adversarial tests of the agent and outreach safety controls (Phase 1.5 audit).

Each test tries to get an external action through a control that should stop it.
"""



import pytest
from sqlalchemy import select

from dealerflow.audit import integration_disabled
from dealerflow.db import utcnow
from dealerflow.integrations.calendar import MockCalendarProvider
from dealerflow.integrations.email import MockEmailProvider
from dealerflow.integrations.errors import CredentialError, ExternalActionsHalted
from dealerflow.integrations.zoom import MockZoomProvider
from dealerflow.jobs import enqueue, run_pending
from dealerflow.models import AuditLog, Campaign, IntegrationLog, Job, Meeting, OutreachMessage, Prospect, Template
from dealerflow.services import agent, meetings, outreach, reports
from dealerflow.services.agent import set_campaign_mode, set_status, tick
from dealerflow.services.discovery import run_source
from dealerflow.services.inbox import poll_inbox_job
from dealerflow.services.prospects import LeadRecord, intake
from tests.conftest import login, make_user
from tests.test_compliance import _prospect, _tpl

IN_HOURS = agent.datetime(2026, 10, 20, 15, 0)


def _queue(db, admin, p=None, **kw):
    p = p or _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=admin, **kw)
    outreach.approve(db, m, actor=admin)
    db.commit()
    return m


def _status(db, mid):
    db.expire_all()
    return db.get(OutreachMessage, mid).status


# --- Emergency stop ------------------------------------------------------------------------------

def test_emergency_stop_blocks_every_external_path(db, admin):
    p = _prospect(db)
    m_req = meetings.open_request(db, p, attendee_email=p.email, source="t")
    slots = meetings.suggest_slots(db, m_req)
    meetings.choose_slot(db, m_req, slots[0]["start"], actor=admin)
    meetings.approve_booking(db, m_req, actor=admin)
    enqueue(db, "poll_inbox", "poll:x", {})
    db.commit()
    set_status(db, "emergency_stopped", actor=admin, reason="adversarial test")
    db.commit()

    run_pending()  # booking + inbox poll jobs
    assert MockZoomProvider.meetings == {} and MockCalendarProvider.events == {}
    db.expire_all()
    jobs = {j.kind: j for j in db.scalars(select(Job))}
    assert jobs["book_meeting"].status == "pending" and "deferred" in jobs["book_meeting"].last_error
    assert jobs["book_meeting"].attempts == 0  # deferral doesn't burn retries
    assert jobs["poll_inbox"].status == "pending"
    # Direct service calls (what UI routes do) are refused too
    with pytest.raises(ExternalActionsHalted):
        meetings.suggest_slots(db, db.get(Meeting, m_req.id))
    with pytest.raises(ExternalActionsHalted):
        run_source(db, "fixture")
    with pytest.raises(ExternalActionsHalted):
        poll_inbox_job(db, {})
    db.rollback()
    blocked = list(db.scalars(select(IntegrationLog).where(IntegrationLog.status == "blocked")))
    assert blocked  # refusals are logged

    # Clearing the stop lets the deferred work finish exactly once
    set_status(db, "running", actor=admin)
    for j in db.scalars(select(Job)):
        j.run_after = utcnow()
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Meeting, m_req.id).status == "booked" and len(MockZoomProvider.meetings) == 1


def test_emergency_stop_blocks_call_and_discovery_buttons(db, client):
    admin = make_user(db)
    run_source(db, "fixture")
    db.commit()
    csrf = login(client, admin.email)
    pid = db.scalar(select(Prospect.id).where(Prospect.phone_e164.is_not(None)))
    client.post("/agent/status", data={"status": "emergency_stopped", "reason": "t", "csrf": csrf})
    r = client.post(f"/prospects/{pid}/call", data={"csrf": csrf})
    assert "Emergency stop" in r.text or "Calling hours" in r.text or "Outside permitted" in r.text
    from dealerflow.integrations.calls import MockCallProvider
    assert MockCallProvider.calls == []
    r = client.post("/discover", data={"source": "fixture", "csrf": csrf})
    assert "Emergency stop" in r.text


# --- Campaign / agent pause, operating hours --------------------------------------------------------

def test_paused_campaign_holds_already_queued_messages(db, admin):
    camp = db.scalar(select(Campaign))
    set_campaign_mode(db, camp, actor=admin, status="active")
    m = _queue(db, admin)
    m.campaign_id = camp.id
    set_campaign_mode(db, camp, actor=admin, status="paused")
    db.commit()
    run_pending()
    assert _status(db, m.id) == "queued" and MockEmailProvider.outbox == []
    set_campaign_mode(db, db.get(Campaign, camp.id), actor=admin, status="active")
    db.scalar(select(Job).where(Job.kind == "send_email")).run_after = utcnow()
    db.commit()
    run_pending()
    assert _status(db, m.id) == "sent"


def test_agent_approved_messages_wait_when_agent_paused_or_out_of_hours(db, admin, monkeypatch):
    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=None, origin="agent")
    outreach.approve(db, m, actor=None, kind="campaign_auto")
    set_status(db, "paused", actor=admin)
    db.commit()
    run_pending()
    assert _status(db, m.id) == "queued"
    set_status(db, "running", actor=admin)
    monkeypatch.setattr(agent, "in_operating_window", lambda cfg, now: False)
    db.scalar(select(Job)).run_after = utcnow()
    db.commit()
    run_pending()
    db.expire_all()
    assert _status(db, m.id) == "queued" and "operating hours" in db.scalar(select(Job)).last_error
    # A human-approved message is not held by agent state (a person made that decision)
    set_status(db, "paused", actor=admin)
    h = _queue(db, admin, _prospect(db, n=2))
    run_pending()
    assert _status(db, h.id) == "sent"


# --- Credentials, providers ---------------------------------------------------------------------

def test_auth_failure_disables_provider_until_reverified(db, admin, client):
    m1 = _queue(db, admin)
    MockEmailProvider.fail_next = [CredentialError("token revoked")]
    run_pending()
    assert integration_disabled(db, "mock_email") is not None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "integration.disabled"))
    assert _status(db, m1.id) == "failed"
    # Next send never reaches the provider
    calls = []
    orig = MockEmailProvider.send
    MockEmailProvider.send = lambda self, msg: calls.append(msg) or orig(self, msg)
    try:
        m2 = _queue(db, admin, _prospect(db, n=2))
        run_pending()
        assert calls == [] and _status(db, m2.id) == "failed"
        # An admin re-verifies: the provider works again
        csrf = login(client, admin.email)
        client.post("/integrations/check", data={"csrf": csrf})
        db.expire_all()
        assert integration_disabled(db, "mock_email") is None
        m3 = _queue(db, admin, _prospect(db, n=3))
        run_pending()
        assert _status(db, m3.id) == "sent"
    finally:
        MockEmailProvider.send = orig


def test_failed_provider_call_inside_job_stays_logged(db, admin):
    """A job that fails rolls back its transaction; the provider failure must still be recorded."""
    p = _prospect(db)
    m = meetings.open_request(db, p, attendee_email=p.email, source="t")
    slots = meetings.suggest_slots(db, m)
    meetings.choose_slot(db, m, slots[0]["start"], actor=admin)
    meetings.approve_booking(db, m, actor=admin)
    db.commit()
    MockZoomProvider.fail_next = [CredentialError("zoom app deactivated")]
    run_pending()
    db.expire_all()
    row = db.scalar(select(IntegrationLog).where(IntegrationLog.provider == "mock_zoom", IntegrationLog.status == "auth_error"))
    assert row is not None and "deactivated" in row.detail
    assert integration_disabled(db, "mock_zoom") is not None


def test_unapproved_provider_name_cannot_send(db, admin, monkeypatch):
    from dealerflow.config import get_settings

    m = _queue(db, admin)
    monkeypatch.setenv("DF_EMAIL_PROVIDER", "sendgrid")
    get_settings.cache_clear()
    run_pending()
    assert _status(db, m.id) == "failed" and MockEmailProvider.outbox == []


def test_real_provider_never_contacts_simulated_or_reserved_addresses(db, admin):
    sim = _prospect(db)
    m = outreach.create_draft(db, sim, _tpl(db), actor=admin)
    from dealerflow.services.compliance import email_blockers
    reasons = email_blockers(db, m, provider_simulated=False)
    assert any("SIMULATED" in r for r in reasons) and any("reserved test domain" in r for r in reasons)
    real = intake(db, LeadRecord(name="Real Lot", source="manual", email="sales@lot.test",
                                 dealership_type="independent_used")).prospect
    m2 = outreach.create_draft(db, real, _tpl(db), actor=admin)
    assert any("reserved test domain" in r for r in email_blockers(db, m2, provider_simulated=False))


# --- Approval cannot be bypassed --------------------------------------------------------------

def test_api_cookie_writes_need_csrf_and_queued_content_is_frozen(db, client):
    admin = make_user(db)
    run_source(db, "fixture")
    db.commit()
    csrf = login(client, admin.email)
    pid = client.get("/api/v1/prospects").json()["items"][0]["id"]
    assert client.post(f"/api/v1/prospects/{pid}/score").status_code == 403  # no token
    assert client.post(f"/api/v1/prospects/{pid}/score", headers={"x-csrf-token": csrf}).status_code == 200
    m = _queue(db, admin, db.get(Prospect, pid))
    r = client.post(f"/outreach/{m.id}/edit", data={"subject": "changed", "body": "changed", "csrf": csrf})
    assert r.status_code == 400 and db.get(OutreachMessage, m.id).subject != "changed"


def test_agent_cannot_auto_approve_flagged_or_person_edited_drafts(db, admin, monkeypatch):
    monkeypatch.setattr(agent, "in_operating_window", lambda cfg, now: True)
    camp = db.scalar(select(Campaign))
    set_status(db, "running", actor=admin)
    set_campaign_mode(db, camp, actor=admin, status="active", approval_mode="auto")
    tpl = db.scalar(select(Template).where(Template.name == "intro", Template.language == "en"))
    tpl.body = tpl.body + "\n\nP.S. {observation}"
    db.commit()
    run = tick(db, now=IN_HOURS)
    db.commit()
    # No verified observation exists, so drafting stops instead of inventing one
    assert run.counts["sequences"]["drafted"] == 0 and run.counts["sequences"]["stopped"] >= 1
    assert db.scalar(select(OutreachMessage)) is None


def test_template_cannot_reach_python_internals(db, admin):
    p = _prospect(db)
    tpl = Template(name="evil", language="en", subject="Hi {dealership_name.__class__.__mro__}", body="x", active=True)
    db.add(tpl)
    db.flush()
    with pytest.raises(outreach.DraftError):
        outreach.create_draft(db, p, tpl, actor=admin)
    assert outreach.validate_template(tpl.subject, tpl.body)


# --- Simulated vs real ---------------------------------------------------------------------------

def test_simulated_activity_never_counts_as_real(db, admin, client):
    from dealerflow.web.routes_agent import agent_metrics

    for i in range(3):
        m = _queue(db, admin, _prospect(db, n=i + 10))
    run_pending()
    MockEmailProvider.inject_reply("owner@dealer10.example", "Re", "Interested, let's set up a Zoom call",
                                   in_reply_to=db.get(OutreachMessage, m.id).rfc_message_id)
    poll_inbox_job(db, {})
    db.commit()
    d = reports.daily(db)
    assert d["sent"]["real"] == 0 and d["sent"]["simulated"] == 3
    assert d["positive"]["real"] == 0
    w = reports.weekly(db)
    assert {f["stage"]: f["count"] for f in w["funnel"]}["Contacted (actually sent)"] == 0
    am = agent_metrics(db)
    assert am["sent"] == 0 and am["sent_simulated"] == 3 and am["positive"] == 0 and am["replies"] == 0


# --- Traceability --------------------------------------------------------------------------------

STATE_CHANGING = {"send_email": "outreach.sent", "create_meeting": "meeting.booked", "create_event": "meeting.booked",
                  "update_meeting": "meeting.reschedule", "update_event": "meeting.reschedule",
                  "delete_meeting": "meeting.cancel", "delete_event": "meeting.cancel", "start_call": "call.start",
                  "send_digest": "notification.digest_sent"}


def test_every_external_state_change_has_an_audit_entry(db, admin):
    p = _prospect(db)
    _queue(db, admin, p)
    run_pending()
    m = meetings.open_request(db, p, attendee_email=p.email, source="t")
    slots = meetings.suggest_slots(db, m)
    meetings.choose_slot(db, m, slots[0]["start"], actor=admin)
    meetings.approve_booking(db, m, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    m = db.get(Meeting, m.id)
    slots = meetings.suggest_slots(db, m)
    meetings.reschedule(db, m, slots[-1]["start"], actor=admin)
    meetings.cancel(db, m, actor=admin, reason="t")
    db.commit()
    actions = {a.action for a in db.scalars(select(AuditLog))}
    used = {r.action for r in db.scalars(select(IntegrationLog).where(IntegrationLog.status == "ok"))}
    for action in used & set(STATE_CHANGING):
        assert STATE_CHANGING[action] in actions, action
    assert {"send_email", "create_meeting", "create_event", "update_meeting", "delete_event"} <= used


