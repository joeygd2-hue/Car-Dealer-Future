"""Opt-outs, suppression, stop conditions, duplicate prevention and send limits."""

import pytest
from sqlalchemy import select

from dealerflow.integrations.email import MockEmailProvider
from dealerflow.jobs import enqueue, run_pending
from dealerflow.models import Enrollment, Job, OutreachMessage, Prospect, Suppression, Template
from dealerflow.security import sign
from dealerflow.services import outreach
from dealerflow.services.compliance import email_blockers, suppress
from dealerflow.services.inbox import poll_inbox_job
from dealerflow.services.prospects import LeadRecord, intake
from tests.conftest import make_user


def _prospect(db, n=1, **kw):
    rec = LeadRecord(name=f"Test Dealer {n} (SIMULATED)", source="fixture", simulated=True, city="Sugar Land",
                     state="TX", website=f"https://dealer{n}.example", email=f"owner@dealer{n}.example",
                     dealership_type="independent_used", **kw)
    return intake(db, rec).prospect


def _tpl(db, name="intro", lang="en"):
    return db.scalar(select(Template).where(Template.name == name, Template.language == lang))


def _send(db, admin, p, tpl=None):
    m = outreach.create_draft(db, p, tpl or _tpl(db), actor=admin)
    outreach.approve(db, m, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    return db.get(OutreachMessage, m.id)


def test_opt_out_reply_suppresses_and_stops_everything(db, admin):
    p = _prospect(db)
    sent = _send(db, admin, p)
    queued = outreach.create_draft(db, p, _tpl(db, "follow_up_1"), actor=admin)
    db.commit()
    MockEmailProvider.inject_reply(p.email, "Re: x", "Please remove me from your list.", in_reply_to=sent.rfc_message_id)
    poll_inbox_job(db, {})
    db.commit()
    p = db.get(Prospect, p.id)
    assert p.do_not_contact and p.stage == "do_not_contact"
    assert db.scalar(select(Suppression).where(Suppression.value == p.email)).reason == "reply_opt_out"
    assert db.get(OutreachMessage, queued.id).status == "cancelled"
    with pytest.raises(outreach.DraftError):
        outreach.create_draft(db, p, _tpl(db), actor=admin)


def test_spanish_opt_out(db, admin):
    p = _prospect(db)
    sent = _send(db, admin, p)
    MockEmailProvider.inject_reply(p.email, "Re: x", "Por favor darme de baja.", in_reply_to=sent.rfc_message_id)
    poll_inbox_job(db, {})
    db.commit()
    assert db.get(Prospect, p.id).do_not_contact


def test_unsubscribe_link_and_one_click(db, admin, client):
    p = _prospect(db)
    db.commit()
    token = sign({"e": p.email, "p": p.id}, "unsubscribe")
    assert client.get(f"/u/{token}").status_code == 200  # confirmation page; GET changes nothing
    assert db.scalar(select(Suppression)) is None
    r = client.post(f"/u/{token}")  # RFC 8058 one-click POST (no session, no CSRF)
    assert r.status_code == 200
    db.expire_all()
    assert db.scalar(select(Suppression).where(Suppression.value == p.email))
    assert db.get(Prospect, p.id).do_not_contact
    assert client.post("/u/forged-token").status_code == 400


def test_suppressed_domain_blocks_draft_approval(db, admin):
    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=admin)
    suppress(db, "@dealer1.example", "manual")
    db.flush()
    assert m.status == "cancelled"
    m.status = "draft"  # even if someone forced it back to draft, approval re-checks the list
    with pytest.raises(outreach.ApprovalError, match="suppression"):
        outreach.approve(db, m, actor=admin)


def test_send_time_recheck_blocks_opt_out_after_approval(db, admin):
    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=admin)
    outreach.approve(db, m, actor=admin)
    # Opt-out arrives between approval and the worker run: suppression cancels the queued message
    db.add(Suppression(value=p.email, kind="email", reason="unsubscribe"))  # raw insert, bypassing suppress()
    db.commit()
    run_pending()
    db.expire_all()
    m = db.get(OutreachMessage, m.id)
    assert m.status == "cancelled" and "suppression" in m.error
    assert MockEmailProvider.outbox == []


def test_duplicate_send_jobs_send_once(db, admin):
    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=admin)
    outreach.approve(db, m, actor=admin)
    job2, created = enqueue(db, "send_email", f"send_email:{m.id}", {"message_id": m.id})
    assert not created
    # Even a second job with a different key must not send twice (handler checks status).
    enqueue(db, "send_email", f"send_email:{m.id}:dup", {"message_id": m.id})
    db.commit()
    run_pending()
    assert len(MockEmailProvider.outbox) == 1
    db.expire_all()
    jobs = list(db.scalars(select(Job).where(Job.kind == "send_email")))
    assert len(jobs) == 2 and {j.status for j in jobs} == {"done"}


def test_touch_limit_cooldown_and_duplicates(db, admin, monkeypatch):
    p = _prospect(db)
    _send(db, admin, p)
    m2 = outreach.create_draft(db, p, _tpl(db, "follow_up_1"), actor=admin)
    assert any("Cooldown" in b for b in email_blockers(db, m2))
    m3 = outreach.create_draft(db, p, _tpl(db), actor=admin)
    assert any("Duplicate" in b for b in email_blockers(db, m3))


def test_reply_stops_sequence(db, admin):
    from dealerflow.models import Campaign
    from dealerflow.services.sequences import enroll

    p = _prospect(db)
    camp = db.scalar(select(Campaign))
    e = enroll(db, camp, p)
    sent = _send(db, admin, p)
    MockEmailProvider.inject_reply(p.email, "Re: hi", "Thanks, what does it cost?", in_reply_to=sent.rfc_message_id)
    poll_inbox_job(db, {})
    db.commit()
    assert db.get(Enrollment, e.id).status == "stopped"
    follow = outreach.create_draft(db, p, _tpl(db, "follow_up_1"), actor=admin)
    assert any("replied" in b for b in email_blockers(db, follow))


def test_missing_sender_identity_blocks(db, admin, monkeypatch):
    from dealerflow.config import get_settings

    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=admin)
    monkeypatch.setenv("DF_BUSINESS_POSTAL_ADDRESS", "")
    get_settings.cache_clear()
    assert any("CAN-SPAM" in b for b in email_blockers(db, m))


def test_rep_cannot_approve(db, admin):
    rep = make_user(db, "rep@test.example", "rep")
    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=rep)
    with pytest.raises(outreach.ApprovalError):
        outreach.approve(db, m, actor=rep)


def test_draft_refuses_missing_verified_values(db, admin):
    p = _prospect(db)
    tpl = Template(name="needs_contact", language="en", subject="Hi {contact_first_name}", body="x", active=True)
    db.add(tpl)
    db.flush()
    with pytest.raises(outreach.DraftError, match="contact_first_name"):
        outreach.create_draft(db, p, tpl, actor=admin)


def test_unverified_observation_cannot_be_used(db, admin):
    from dealerflow.models import Evidence

    p = _prospect(db)
    ev = Evidence(prospect=p, field="signal:chat_widget", value="false", source_type="website", note="No chat widget seen")
    db.add(ev)
    tpl = Template(name="obs", language="en", subject="Idea for {dealership_name}", body="I noticed: {observation}", active=True)
    db.add(tpl)
    db.flush()
    with pytest.raises(outreach.DraftError, match="not been verified"):
        outreach.create_draft(db, p, tpl, actor=admin, observation_evidence_id=ev.id)
    ev.verified = True
    m = outreach.create_draft(db, p, tpl, actor=admin, observation_evidence_id=ev.id)
    assert "No chat widget seen" in m.body and m.review_flags == ["uses {observation}"]


def test_daily_cap_defers_real_sends(db, admin, monkeypatch):
    """With a real provider, the daily cap defers the job to tomorrow instead of failing it."""
    from dealerflow.config import get_settings
    from dealerflow.services.agent import authorize_live

    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=admin)
    m.simulated = False
    outreach.approve(db, m, actor=admin)
    authorize_live(db, actor=admin, authorize=True)
    db.commit()
    monkeypatch.setenv("DF_OUTREACH_DAILY_CAP", "0")
    monkeypatch.setenv("DF_OUTREACH_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    run_pending()
    db.expire_all()
    job = db.scalar(select(Job).where(Job.key == f"send_email:{m.id}"))
    assert job.status == "pending" and "daily cap" in job.last_error and job.attempts == 0
    assert MockEmailProvider.outbox == []
