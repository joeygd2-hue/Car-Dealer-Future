"""Customer messaging channels (Phase 3): who can deliver a reply to a car buyer, and how.

Resolution rule (``resolve``): a reply is delivered for real only when ALL hold —
  * ``DF_LIVE_CUSTOMER_MESSAGING_ENABLED=true`` (default false);
  * the client has a ``customer_messaging`` integration connection for that provider with status ``connected``
    (set by staff only after verification / documented authorization — Phase 4 onboarding);
  * the provider is first-party website chat. Messenger and Twilio live adapters (with encrypted per-client
    credentials) are not wired in this build, so they always resolve to the sandbox.
Otherwise the SANDBOX channel is used: nothing leaves the system, the attempt is written to
``integration_log`` as simulated and the message is marked SIMULATED.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import log_integration
from ..config import get_settings
from ..models_ops import IntegrationConnection

WEBSITE_CHAT = "WEBSITE_CHAT"
META_MESSENGER = "META_MESSENGER"
TWILIO_SMS = "TWILIO_SMS"
ADF_LEAD = "ADF_LEAD"
MARKETPLACE_ASSISTED = "MARKETPLACE_ASSISTED"

CUSTOMER_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS, ADF_LEAD, MARKETPLACE_ASSISTED)
# Channels where the platform itself can deliver a reply. Others are sent by staff outside the system.
SENDABLE_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS)
CONNECTION_KIND = "customer_messaging"
PROVIDER_NAME = {WEBSITE_CHAT: "website_chat", META_MESSENGER: "meta_messenger", TWILIO_SMS: "twilio_sms",
                 ADF_LEAD: "adf_lead", MARKETPLACE_ASSISTED: "marketplace_assisted"}

SENT, SIMULATED, FAILED = "SENT", "SIMULATED", "FAILED"


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


def _sandbox_failure(mode: str | None) -> SendResult | None:
    if mode == "retryable":
        return SendResult(FAILED, error="sandbox: simulated transient provider error (HTTP 503)", retryable=True)
    if mode == "permanent":
        return SendResult(FAILED, error="sandbox: simulated permanent provider error (HTTP 400)", retryable=False)
    if mode == "uncertain":
        return SendResult(FAILED, error="sandbox: simulated read timeout after request was sent", uncertain=True)
    return None


class SandboxCustomerChannel:
    """Records the reply instead of delivering it. ``failure_mode`` (connection config) exercises error paths."""

    supports_send = True

    def __init__(self, db: Session, provider: str, failure_mode: str | None = None):
        self.db, self.provider, self.failure_mode = db, provider, failure_mode

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        failed = _sandbox_failure(self.failure_mode)
        if failed:
            log_integration(self.db, f"sandbox:{self.provider}", "customer_send", "error", detail=failed.error or "", simulated=True)
            return failed
        mid = f"sandbox-{uuid.uuid4().hex[:12]}"
        log_integration(self.db, f"sandbox:{self.provider}", "customer_send", "ok", simulated=True,
                        detail=json.dumps({"id": mid, "recipient": recipient_id, "text": text, "human_agent": human_agent}))
        return SendResult(SIMULATED, provider_message_id=mid)


class FirstPartyChat:
    """Website chat: the reply is stored and the dealership's own widget fetches it."""

    provider = WEBSITE_CHAT
    supports_send = True

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        return SendResult(SENT)


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


def connection(db: Session, client_id: int, channel: str) -> IntegrationConnection | None:
    return db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.client_id == client_id, IntegrationConnection.kind == CONNECTION_KIND,
        IntegrationConnection.provider == PROVIDER_NAME.get(channel, channel)))


def resolve(db: Session, client_id: int, channel: str) -> ResolvedChannel:
    conn = connection(db, client_id, channel)
    if channel in (MARKETPLACE_ASSISTED, ADF_LEAD):
        return ResolvedChannel(ManualOnlyChannel(channel), False, "manual channel")
    failure_mode = (conn.config or {}).get("sandbox_failure_mode") if conn else None
    live_switch = get_settings().live_customer_messaging_enabled
    if channel == WEBSITE_CHAT and live_switch and conn is not None and conn.status == "connected":
        return ResolvedChannel(FirstPartyChat(), True, "first-party website chat (connection authorized)")
    if not live_switch:
        why = "DF_LIVE_CUSTOMER_MESSAGING_ENABLED is false"
    elif conn is None or conn.status != "connected":
        why = f"no authorized {PROVIDER_NAME.get(channel, channel)} connection for this dealership"
    else:
        why = f"live {PROVIDER_NAME.get(channel, channel)} delivery is not available in this build"
    return ResolvedChannel(SandboxCustomerChannel(db, channel, failure_mode), False, f"sandbox: {why}")
