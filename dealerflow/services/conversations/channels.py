"""Customer channels, the per-dealership test-drive calendar and webhook signature helpers.

Every adapter here is a sandbox: a reply "sent" on a sendable channel is recorded as SIMULATED in the
integration log and never leaves the platform; website-chat visitors only ever see replies marked SENT, so a
sandbox reply is not shown to them. Phase 3's live Messenger and Twilio send adapters were not ported (they need
Meta App Review, Twilio A2P registration and live authorization; see docs/progress/known-limitations.md).

Channel connections are Phase 4 ``integration_connections`` rows: ``kind="customer_messaging"`` with
``provider`` = the lower-case channel code, and ``kind="calendar"`` for a dealership's test-drive calendar.
``config["sandbox_failure_mode"]`` ("retryable" | "permanent" | "uncertain") makes a sandbox adapter fail on
purpose so the error paths can be exercised.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...audit import track_integration
from ...models import Appointment, IntegrationConnection

WEBSITE_CHAT = "WEBSITE_CHAT"
META_MESSENGER = "META_MESSENGER"
TWILIO_SMS = "TWILIO_SMS"
ADF_LEAD = "ADF_LEAD"
MARKETPLACE_ASSISTED = "MARKETPLACE_ASSISTED"

CUSTOMER_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS, ADF_LEAD, MARKETPLACE_ASSISTED)
# Channels where the platform itself can deliver a reply. Others are staff-sent outside the system.
SENDABLE_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS)
CHANNEL_LABELS = {WEBSITE_CHAT: "Website chat", META_MESSENGER: "Messenger", TWILIO_SMS: "SMS",
                  ADF_LEAD: "ADF lead", MARKETPLACE_ASSISTED: "Marketplace (manual)"}

SENT, SIMULATED, FAILED = "SENT", "SIMULATED", "FAILED"


class CalendarError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


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


def _fail(failure_mode: str | None) -> SendResult | None:
    if failure_mode == "retryable":
        return SendResult(FAILED, error="sandbox: simulated transient provider error (HTTP 503)", retryable=True)
    if failure_mode == "permanent":
        return SendResult(FAILED, error="sandbox: simulated permanent provider error (HTTP 400)", retryable=False)
    if failure_mode == "uncertain":
        return SendResult(FAILED, error="sandbox: simulated read timeout after request was sent", uncertain=True)
    return None


class SandboxCustomerChannel:
    supports_send = True

    def __init__(self, db: Session, provider: str, failure_mode: str | None = None):
        self.db = db
        self.provider = provider
        self.failure_mode = failure_mode

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        with track_integration(self.db, f"customer:{self.provider.lower()}", "send_text", simulated=True) as log:
            failed = _fail(self.failure_mode)
            mid = None if failed else f"sandbox-{uuid.uuid4().hex[:12]}"
            log["detail"] = failed.error if failed else f"SIMULATED reply to {recipient_id[:40]} ({len(text)} chars) id={mid}"
        return failed or SendResult(SIMULATED, provider_message_id=mid)


class ManualOnlyChannel:
    """Marketplace (no API) and ADF leads: staff send the reply outside the platform."""

    supports_send = False

    def __init__(self, provider: str):
        self.provider = provider

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        return SendResult(FAILED, error=f"{self.provider} replies must be sent manually by staff", retryable=False)


@dataclass
class ResolvedChannel:
    adapter: object
    live: bool
    note: str


def channel_connection(db: Session, client_id: int, channel: str) -> IntegrationConnection | None:
    return db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.client_id == client_id, IntegrationConnection.kind == "customer_messaging",
        IntegrationConnection.provider == channel.lower()))


def customer_channel(db: Session, client_id: int, channel: str) -> ResolvedChannel:
    if channel in (MARKETPLACE_ASSISTED, ADF_LEAD):
        return ResolvedChannel(ManualOnlyChannel(channel), False, "manual channel")
    conn = channel_connection(db, client_id, channel)
    failure_mode = (conn.config or {}).get("sandbox_failure_mode") if conn else None
    return ResolvedChannel(SandboxCustomerChannel(db, channel, failure_mode), False,
                           "sandbox: live customer messaging is not available in this build")


# --- Per-dealership test-drive calendar ------------------------------------------------------------------------


@dataclass
class BusyInterval:
    start: datetime
    end: datetime


class SandboxDealerCalendar:
    """Sandbox test-drive calendar: busy = the dealership's own confirmed appointments; events are simulated."""

    name = "sandbox_calendar"
    simulated = True

    def __init__(self, db: Session, client_id: int, failure_mode: str | None = None):
        self.db = db
        self.client_id = client_id
        self.failure_mode = failure_mode

    def _check(self) -> None:
        if self.failure_mode:
            raise CalendarError("sandbox: simulated calendar failure", retryable=self.failure_mode == "retryable")

    def free_busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        with track_integration(self.db, "dealer_calendar", "free_busy", simulated=True):
            self._check()
            busy = []
            for a in self.db.scalars(select(Appointment).where(Appointment.client_id == self.client_id,
                                                               Appointment.status == "CONFIRMED")):
                s = a.confirmed_start or a.requested_start
                e = s + timedelta(minutes=a.duration_minutes)
                if s < end and e > start:
                    busy.append(BusyInterval(s, e))
            return busy

    def create_event(self, *, summary: str, start: datetime, end: datetime, description: str,
                     location: str | None = None) -> str:
        with track_integration(self.db, "dealer_calendar", "create_event", simulated=True) as log:
            self._check()
            eid = f"sandbox-evt-{uuid.uuid4().hex[:10]}"
            log["detail"] = f"SIMULATED event {eid} at {start.isoformat()}"
            return eid

    def cancel_event(self, event_id: str) -> None:
        with track_integration(self.db, "dealer_calendar", "cancel_event", simulated=True) as log:
            self._check()
            log["detail"] = f"SIMULATED cancel {event_id}"


def dealership_calendar(db: Session, client_id: int) -> SandboxDealerCalendar | None:
    """The dealership's test-drive calendar, or None when no calendar connection is set up."""
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == client_id,
                                                         IntegrationConnection.kind == "calendar"))
    if conn is None or conn.status in ("not_connected", "disabled"):
        return None
    return SandboxDealerCalendar(db, client_id, (conn.config or {}).get("sandbox_failure_mode"))


# --- Webhook signatures -----------------------------------------------------------------------------------------


def verify_meta_signature(app_secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """Validate ``X-Hub-Signature-256: sha256=<hex hmac>``."""
    if not app_secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))


def parse_meta_webhook(payload: dict) -> list[dict]:
    """Normalize a Page webhook payload into inbound message events (echoes, reads and deliveries skipped)."""
    events = []
    if payload.get("object") != "page":
        return events
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id", ""))
        for item in entry.get("messaging", []):
            msg = item.get("message") or {}
            if not msg or msg.get("is_echo") or "mid" not in msg:
                continue
            events.append({"page_id": page_id, "sender_id": str(item.get("sender", {}).get("id", "")),
                           "mid": msg["mid"], "text": msg.get("text") or ("[attachment]" if msg.get("attachments") else ""),
                           "timestamp_ms": item.get("timestamp")})
    return events


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio request signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def verify_twilio_signature(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)
