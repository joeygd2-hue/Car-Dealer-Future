"""Customer channels, the dealership calendar, staff email and the optional LLM for the response engine.

Everything defaults to a SIMULATED (sandbox) adapter. A sandbox adapter records what it would have sent in
``sandbox_records`` and returns status SIMULATED; nothing leaves the system.

- WEBSITE_CHAT is first-party: a reply is "delivered" by storing it for the dealership's own chat widget. It is
  only treated as live (shown to the visitor) when ``runtime.live_authorized`` is true for the client.
- ADF_LEAD and MARKETPLACE_ASSISTED have no reply API: staff answer outside the platform and record it.
- META_MESSENGER and TWILIO_SMS: the Phase 3 live adapters were NOT ported (they relied on per-tenant encrypted
  credentials, which this app does not store). Replies on these channels are always SIMULATED here.
- Staff notification email is always SIMULATED in this release.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import IntegrationConnection, SandboxRecord
from . import runtime

WEBSITE_CHAT = "WEBSITE_CHAT"
META_MESSENGER = "META_MESSENGER"
TWILIO_SMS = "TWILIO_SMS"
ADF_LEAD = "ADF_LEAD"
MARKETPLACE_ASSISTED = "MARKETPLACE_ASSISTED"
CUSTOMER_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS, ADF_LEAD, MARKETPLACE_ASSISTED)
SENDABLE_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS)
CHANNEL_LABELS = {WEBSITE_CHAT: "Website chat", META_MESSENGER: "Facebook Messenger", TWILIO_SMS: "SMS",
                  ADF_LEAD: "ADF lead (email/CRM)", MARKETPLACE_ASSISTED: "Facebook Marketplace (staff-assisted)"}

SENT = "SENT"
SIMULATED = "SIMULATED"  # sandbox: nothing left the system
FAILED = "FAILED"


@dataclass
class SendResult:
    status: str
    provider_message_id: str | None = None
    error: str | None = None
    retryable: bool = False
    # True when the request may have reached the provider (e.g. read timeout after sending): never auto-retry.
    uncertain: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (SENT, SIMULATED)

    @property
    def simulated(self) -> bool:
        return self.status == SIMULATED


class CalendarError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class BusyInterval:
    start: datetime
    end: datetime


def _fail(failure_mode: str | None) -> SendResult | None:
    """Sandbox failure injection (connection ``config.sandbox_failure_mode``) to exercise error handling."""
    if failure_mode == "retryable":
        return SendResult(FAILED, error="sandbox: simulated transient provider error (HTTP 503)", retryable=True)
    if failure_mode == "permanent":
        return SendResult(FAILED, error="sandbox: simulated permanent provider error (HTTP 400)", retryable=False)
    if failure_mode == "uncertain":
        return SendResult(FAILED, error="sandbox: simulated read timeout after request was sent", uncertain=True)
    return None


class FirstPartyChat:
    """Website chat: the reply is stored and the dealership's own widget fetches it."""

    provider = WEBSITE_CHAT
    supports_send = True

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        return SendResult(SENT, provider_message_id=None)


class ManualOnlyChannel:
    """Marketplace (no API) and ADF leads: staff send the reply outside the platform."""

    supports_send = False

    def __init__(self, provider: str):
        self.provider = provider

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        return SendResult(FAILED, error=f"{self.provider} replies must be sent manually by staff", retryable=False)


class SandboxCustomerChannel:
    supports_send = True

    def __init__(self, db: Session, provider: str, failure_mode: str | None = None):
        self.db = db
        self.provider = provider
        self.failure_mode = failure_mode

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        failed = _fail(self.failure_mode)
        if failed:
            return failed
        mid = f"sandbox-{uuid.uuid4().hex[:12]}"
        self.db.add(SandboxRecord(kind="CHANNEL_OUT", payload={
            "provider": self.provider, "recipient": recipient_id, "text": text, "id": mid, "human_agent": human_agent}))
        return SendResult(SIMULATED, provider_message_id=mid)


@dataclass
class ResolvedChannel:
    adapter: object
    live: bool
    note: str


def customer_channel(db: Session, connection: IntegrationConnection | None, provider: str,
                     client_id: int | None = None) -> ResolvedChannel:
    client_id = client_id if client_id is not None else (connection.client_id if connection else None)
    if provider in (MARKETPLACE_ASSISTED, ADF_LEAD):
        return ResolvedChannel(ManualOnlyChannel(provider), False, "manual channel")
    if provider == WEBSITE_CHAT and runtime.live_authorized(db, client_id, provider):
        return ResolvedChannel(FirstPartyChat(), True, "first-party website chat (live authorized)")
    failure_mode = (connection.config or {}).get("sandbox_failure_mode") if connection else None
    note = ("sandbox: website chat replies are not live-authorized for this dealership" if provider == WEBSITE_CHAT
            else f"sandbox: {provider} live sending is not available in this release")
    return ResolvedChannel(SandboxCustomerChannel(db, provider, failure_mode), False, note)


class SandboxCalendar:
    name = "sandbox_calendar"
    simulated = True

    def __init__(self, db: Session, calendar_key: str, failure_mode: str | None = None):
        self.db = db
        self.calendar_key = calendar_key
        self.failure_mode = failure_mode

    def _events(self):
        rows = self.db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "CAL_EVENT"))
        return [r for r in rows if r.payload.get("calendar") == self.calendar_key and not r.payload.get("cancelled")]

    def free_busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        if self.failure_mode:
            raise CalendarError("sandbox: simulated calendar failure", retryable=self.failure_mode == "retryable")
        busy = []
        for r in self._events():
            s, e = datetime.fromisoformat(r.payload["start"]), datetime.fromisoformat(r.payload["end"])
            if s < end and e > start:
                busy.append(BusyInterval(s, e))
        return busy

    def create_event(self, *, summary, start, end, description, attendees, location=None) -> str:
        if self.failure_mode:
            raise CalendarError("sandbox: simulated calendar failure", retryable=self.failure_mode == "retryable")
        eid = f"sandbox-evt-{uuid.uuid4().hex[:10]}"
        self.db.add(SandboxRecord(kind="CAL_EVENT", payload={
            "calendar": self.calendar_key, "id": eid, "summary": summary, "start": start.isoformat(),
            "end": end.isoformat(), "description": description, "attendees": attendees, "location": location}))
        self.db.flush()
        return eid

    def cancel_event(self, event_id: str) -> None:
        for r in self.db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "CAL_EVENT")):
            if r.payload.get("id") == event_id:
                r.payload = {**r.payload, "cancelled": True}


def dealership_calendar(db: Session, client_id: int) -> SandboxCalendar | None:
    """The dealership's test-drive calendar: a SIMULATED calendar when a ``calendar`` connection exists, else None."""
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == client_id,
                                                         IntegrationConnection.kind == "calendar"))
    if conn is None or conn.status in ("disabled", "not_connected"):
        return None
    return SandboxCalendar(db, f"dealership:{client_id}", (conn.config or {}).get("sandbox_failure_mode"))


class SandboxEmailSender:
    name = "sandbox_email"

    def __init__(self, db: Session):
        self.db = db

    def send(self, *, to, subject, body, from_addr, from_name=None) -> SendResult:
        mid = f"<sandbox-{uuid.uuid4().hex}@sandbox.invalid>"
        self.db.add(SandboxRecord(kind="EMAIL_OUT", payload={"to": to, "subject": subject, "body": body,
                                                             "from": from_addr, "message_id": mid}))
        return SendResult(SIMULATED, provider_message_id=mid)


def notification_email_sender(db: Session) -> tuple[SandboxEmailSender, bool]:
    """(sender, live). Staff notification email is always SIMULATED in this release."""
    return SandboxEmailSender(db), False


class GatewayLLM:
    """Adapter from the engine's ``complete(system, prompt) -> (text, cost_cents)`` to the shared Claude gateway.

    The gateway records token usage as estimated cost entries. Any failure returns no text, so the grounded
    template reply is used unchanged."""

    def __init__(self, db: Session, client_id: int):
        self.db = db
        self.client_id = client_id

    def complete(self, *, system: str, prompt: str, max_tokens: int = 800) -> tuple[str | None, float]:
        from ...integrations import llm

        try:
            res = llm.complete(self.db, system=system, prompt=prompt, feature="customer_response_rephrase",
                               client_id=self.client_id, max_tokens=max_tokens, effort="low")
        except llm.AIUnavailable:
            return None, 0.0
        if res.refused or not res.text.strip():
            return None, res.estimated_cost * 100
        return res.text.strip(), res.estimated_cost * 100


def llm(db: Session, client_id: int) -> GatewayLLM | None:
    from ...integrations import llm as gateway

    return GatewayLLM(db, client_id) if gateway.ai_available() else None
