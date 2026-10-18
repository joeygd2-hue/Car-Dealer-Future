"""live-check sends only to allow-listed inboxes, and only with an explicit confirmation."""

import pytest
from sqlalchemy import select

from dealerflow import livecheck
from dealerflow.config import get_settings
from dealerflow.integrations import email as email_mod
from dealerflow.models import OutreachMessage


class FakeReal(email_mod.MockEmailProvider):
    name, simulated = "fake_real", False


@pytest.fixture
def real_email(monkeypatch):
    FakeReal.outbox = []
    monkeypatch.setenv("DF_EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("DF_TEST_INBOXES", "owner@owner-controlled.com")
    get_settings.cache_clear()
    monkeypatch.setattr("dealerflow.services.outreach.get_email_provider", lambda: FakeReal())
    monkeypatch.setattr("dealerflow.services.outreach.get_email_provider_name", lambda: "smtp")
    from dealerflow import handlers  # noqa: F401


def test_livecheck_refuses_unlisted_or_unconfirmed(real_email):
    with pytest.raises(SystemExit, match="DF_TEST_INBOXES"):
        livecheck.send("prospect@dealer.com", confirmed=True)
    with pytest.raises(SystemExit, match="confirm"):
        livecheck.send("owner@owner-controlled.com", confirmed=False)
    assert FakeReal.outbox == []


def test_livecheck_sends_to_controlled_inbox_without_live_authorization(real_email, db):
    res = livecheck.send("owner@owner-controlled.com", confirmed=True)
    assert res["message_status"] == "sent" and len(FakeReal.outbox) == 1
    out, _ = FakeReal.outbox[0]
    assert out.subject.startswith("[dealerflow live check]") and out.unsubscribe_url
    # A normal message to a non-listed address with the same real provider is still held
    m = db.scalar(select(OutreachMessage))
    assert m.origin == "live_check" and m.simulated  # never counted as a business result
    from dealerflow.services import reports
    assert reports.daily(db)["sent"]["real"] == 0
