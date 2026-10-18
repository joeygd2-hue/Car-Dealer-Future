"""Failed APIs, expired credentials, retries and backoff."""

import httpx
import pytest
from sqlalchemy import select

from dealerflow.db import utcnow
from dealerflow.integrations import http
from dealerflow.integrations.calendar import MockCalendarProvider
from dealerflow.integrations.email import MockEmailProvider
from dealerflow.integrations.errors import CredentialError, IntegrationError, PermanentError, RateLimitedError
from dealerflow.integrations.zoom import MockZoomProvider
from dealerflow.jobs import enqueue, run_job, run_pending
from dealerflow.models import IntegrationLog, Job, Meeting, OutreachMessage
from dealerflow.services import meetings, outreach
from tests.test_compliance import _prospect, _tpl


def _queued(db, admin):
    p = _prospect(db)
    m = outreach.create_draft(db, p, _tpl(db), actor=admin)
    outreach.approve(db, m, actor=admin)
    db.commit()
    return m


def _job(db, key):
    db.expire_all()
    return db.scalar(select(Job).where(Job.key == key))


def test_transient_failure_retries_with_backoff(db, admin):
    m = _queued(db, admin)
    MockEmailProvider.fail_next = [RateLimitedError("429 from provider", retry_after=120)]
    run_pending()
    job = _job(db, f"send_email:{m.id}")
    assert job.status == "pending" and job.attempts == 1 and job.run_after > utcnow()
    assert db.get(OutreachMessage, m.id).status == "queued"  # refused before sending -> safe to retry
    log = db.scalar(select(IntegrationLog).where(IntegrationLog.status == "rate_limited"))
    assert log is not None
    job.run_after = utcnow()
    db.commit()
    run_pending()
    assert _job(db, f"send_email:{m.id}").status == "done"
    assert len(MockEmailProvider.outbox) == 1


def test_expired_credentials_fail_fast_and_are_reported(db, admin):
    m = _queued(db, admin)
    MockEmailProvider.fail_next = [CredentialError("token expired")]
    run_pending()
    job = _job(db, f"send_email:{m.id}")
    assert job.status == "failed" and job.attempts == 1  # not retried until fixed
    msg = db.get(OutreachMessage, m.id)
    assert msg.status == "failed" and "token expired" in msg.error
    assert db.scalar(select(IntegrationLog).where(IntegrationLog.status == "auth_error"))


def test_provider_error_during_send_marks_failed_not_resent(db, admin):
    m = _queued(db, admin)
    MockEmailProvider.fail_next = [IntegrationError("SMTP 554 rejected")]
    run_pending()
    db.expire_all()
    assert db.get(OutreachMessage, m.id).status == "failed"
    assert _job(db, f"send_email:{m.id}").status == "done"
    run_pending()
    assert MockEmailProvider.outbox == []


def test_interrupted_send_is_not_blindly_resent(db, admin):
    m = _queued(db, admin)
    msg = db.get(OutreachMessage, m.id)
    msg.status = "sending"  # a worker crashed after calling the provider
    db.commit()
    run_pending()
    db.expire_all()
    msg = db.get(OutreachMessage, m.id)
    assert msg.status == "failed" and "Outcome unknown" in msg.error
    assert MockEmailProvider.outbox == []


def test_stale_running_job_is_recovered(db):
    job, _ = enqueue(db, "agent_tick", "agent_tick:test", {})
    job.status, job.locked_at = "running", utcnow().replace(year=2020)
    db.commit()
    counts = run_pending()
    assert counts.get("done") == 1


def test_claim_is_exclusive(db):
    job, _ = enqueue(db, "agent_tick", "agent_tick:once", {})
    db.commit()
    assert run_job(job.id) == "done"
    assert run_job(job.id) == "skipped"


def test_meeting_not_booked_when_zoom_fails(db, admin):
    p = _prospect(db)
    m = meetings.open_request(db, p, attendee_email=p.email, source="test")
    slots = meetings.suggest_slots(db, m)
    meetings.choose_slot(db, m, slots[0]["start"], actor=admin)
    meetings.approve_booking(db, m, actor=admin)
    db.commit()
    MockZoomProvider.fail_next = [CredentialError("zoom token invalid")]
    run_pending()
    db.expire_all()
    m = db.get(Meeting, m.id)
    assert m.status == "failed" and m.zoom_meeting_id is None and not MockCalendarProvider.events
    assert p.stage != "meeting_booked"


def test_calendar_failure_retry_reuses_zoom_meeting(db, admin):
    p = _prospect(db)
    m = meetings.open_request(db, p, attendee_email=p.email, source="test")
    slots = meetings.suggest_slots(db, m)
    meetings.choose_slot(db, m, slots[0]["start"], actor=admin)
    meetings.approve_booking(db, m, actor=admin)
    db.commit()
    # 1st call (freebusy recheck) ok, then create_event fails transiently
    MockCalendarProvider.fail_next = []
    orig = MockCalendarProvider.create_event
    calls = {"n": 0}

    def flaky(self, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrationError("calendar 503")
        return orig(self, **kw)

    MockCalendarProvider.create_event = flaky
    try:
        run_pending()
        db.expire_all()
        m = db.get(Meeting, m.id)
        assert m.status == "pending_approval" and m.zoom_meeting_id  # Zoom created, not yet booked
        job = db.scalar(select(Job).where(Job.kind == "book_meeting"))
        job.run_after = utcnow()
        db.commit()
        run_pending()
        db.expire_all()
        m = db.get(Meeting, m.id)
        assert m.status == "booked" and len(MockZoomProvider.meetings) == 1
    finally:
        MockCalendarProvider.create_event = orig


def test_availability_failure_offers_no_times(db, admin):
    p = _prospect(db)
    m = meetings.open_request(db, p, attendee_email=p.email, source="test")
    MockCalendarProvider.fail_next = [IntegrationError("calendar down")]
    with pytest.raises(IntegrationError):
        meetings.suggest_slots(db, m)
    assert not m.proposed_slots
    with pytest.raises(meetings.MeetingError):
        meetings.choose_slot(db, m, "2030-01-01T15:00:00", actor=admin)


def _transport(responses):
    seq = list(responses)

    def handler(request):
        return seq.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_retries_5xx_then_succeeds():
    c = _transport([httpx.Response(503), httpx.Response(200, json={"ok": True})])
    assert http.request("GET", "https://x.test/", provider="t1", client=c).json() == {"ok": True}


def test_http_401_is_credential_error():
    c = _transport([httpx.Response(401, text="expired")])
    with pytest.raises(CredentialError):
        http.request("GET", "https://x.test/", provider="t2", client=c)


def test_http_persistent_429_raises_rate_limited():
    c = _transport([httpx.Response(429, headers={"Retry-After": "1"})] * 3)
    with pytest.raises(RateLimitedError):
        http.request("GET", "https://x.test/", provider="t3", client=c, retries=2)


def test_http_400_is_permanent():
    c = _transport([httpx.Response(400, text="bad")])
    with pytest.raises(PermanentError):
        http.request("GET", "https://x.test/", provider="t4", client=c)


def test_google_invalid_grant_maps_to_credential_error(monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.integrations import google_auth

    monkeypatch.setenv("DF_GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("DF_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DF_GOOGLE_REFRESH_TOKEN", "revoked")
    get_settings.cache_clear()
    google_auth.clear_cache()

    def fake_request(*a, **kw):
        raise PermanentError('google_oauth: HTTP 400: {"error": "invalid_grant"}')

    monkeypatch.setattr(google_auth, "request", fake_request)
    with pytest.raises(CredentialError, match="re-run"):
        google_auth.access_token()


def test_missing_credentials_are_reported(monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.integrations.errors import NotConfiguredError
    from dealerflow.integrations.zoom import get_zoom_provider

    monkeypatch.setenv("DF_ZOOM_PROVIDER", "zoom")
    get_settings.cache_clear()
    with pytest.raises(NotConfiguredError):
        get_zoom_provider()




def test_lost_zoom_response_does_not_create_a_second_meeting(db, admin):
    """Zoom created the meeting but the response was lost: the retry finds it instead of creating another."""
    p = _prospect(db)
    m = meetings.open_request(db, p, attendee_email=p.email, source="test")
    slots = meetings.suggest_slots(db, m)
    meetings.choose_slot(db, m, slots[0]["start"], actor=admin)
    meetings.approve_booking(db, m, actor=admin)
    db.commit()
    orig = MockZoomProvider.create_meeting

    def create_then_timeout(self, **kw):
        orig(self, **kw)
        raise IntegrationError("read timeout")

    MockZoomProvider.create_meeting = create_then_timeout
    try:
        run_pending()
    finally:
        MockZoomProvider.create_meeting = orig
    assert len(MockZoomProvider.meetings) == 1
    db.expire_all()
    job = db.scalar(select(Job).where(Job.kind == "book_meeting"))
    assert job.status == "pending"
    job.run_after = utcnow()
    db.commit()
    run_pending()
    db.expire_all()
    m = db.get(Meeting, m.id)
    assert m.status == "booked" and len(MockZoomProvider.meetings) == 1
    assert m.provider_response["zoom"]["recovered"] is True
