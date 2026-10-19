"""Customer messaging channels (Phase 3): who can deliver a reply to a car buyer, and how.

Resolution rule (``resolve``): a reply is delivered for real only when ALL hold —
  * ``DF_LIVE_CUSTOMER_MESSAGING_ENABLED=true`` (default false);
  * the client has a ``customer_messaging`` integration connection for that provider with status ``connected``
    (set by staff only after verification / documented authorization — Phase 4 onboarding);
  * for Messenger / Twilio, the provider credentials are present in the environment variables the connection's
    ``config`` names (``token_env``; Twilio also ``account_sid_env``). Secrets are never stored in the database.
Live sends also pass the send gate (emergency stop; pilot-mode allowlist) in ``services/customer/outbound.py``.
Otherwise the SANDBOX channel is used: nothing leaves the system, the attempt is written to
``integration_log`` as simulated and the message is marked SIMULATED.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field

import httpx
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


class MessengerChannel:
    """Meta Send API (Page). Requires App Review for pages_messaging; tested only against mocked HTTP."""

    supports_send = True

    def __init__(self, page_id: str, page_access_token: str, graph_version: str, client: httpx.Client | None = None):
        self.url = f"https://graph.facebook.com/{graph_version}/{page_id}/messages"
        self.token = page_access_token
        self.client = client or httpx.Client(timeout=15)

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        body: dict = {"recipient": {"id": recipient_id}, "message": {"text": text},
                      "messaging_type": "MESSAGE_TAG" if human_agent else "RESPONSE"}
        if human_agent:  # staff reply after the 24 h window (needs the Human Agent permission)
            body["tag"] = "HUMAN_AGENT"
        try:
            resp = self.client.post(self.url, params={"access_token": self.token}, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # never reached the provider
            return SendResult(FAILED, error=f"connection error: {exc}", retryable=True)
        except httpx.HTTPError as exc:  # timeout / dropped connection after sending: outcome unknown
            return SendResult(FAILED, error=f"delivery uncertain: {exc}", uncertain=True)
        data = _json(resp)
        if resp.status_code == 200 and data.get("message_id"):
            return SendResult(SENT, provider_message_id=data["message_id"])
        err = data.get("error", {}) if isinstance(data.get("error"), dict) else {}
        retryable = resp.status_code >= 500 or resp.status_code == 429 or err.get("code") in (1, 2, 4, 32, 613)
        return SendResult(FAILED, error=f"Meta API {resp.status_code}: {err.get('message', resp.text[:200])}", retryable=retryable)


class TwilioSmsChannel:
    """Twilio Messages API. Requires A2P 10DLC registration; tested only against mocked HTTP."""

    supports_send = True

    def __init__(self, account_sid: str, auth_token: str, from_number: str, client: httpx.Client | None = None):
        self.sid, self.token, self.from_number = account_sid, auth_token, from_number
        self.client = client or httpx.Client(timeout=15)

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        try:
            resp = self.client.post(url, auth=(self.sid, self.token), data={"To": recipient_id, "From": self.from_number, "Body": text})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return SendResult(FAILED, error=f"connection error: {exc}", retryable=True)
        except httpx.HTTPError as exc:
            return SendResult(FAILED, error=f"delivery uncertain: {exc}", uncertain=True)
        data = _json(resp)
        if resp.status_code in (200, 201) and data.get("sid"):
            return SendResult(SENT, provider_message_id=data["sid"])
        # 21610 = recipient replied STOP: permanent; the caller records the opt-out.
        return SendResult(FAILED, error=f"Twilio {resp.status_code} code={data.get('code')}: {data.get('message', '')}",
                          retryable=resp.status_code >= 500 or resp.status_code == 429)


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _env(conn: IntegrationConnection, key: str) -> str:
    name = (conn.config or {}).get(key) or ""
    return os.environ.get(name, "") if name.startswith("DF_") else ""


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
    if live_switch and conn is not None and conn.status == "connected" and conn.external_account_id:
        if channel == META_MESSENGER and _env(conn, "token_env"):
            return ResolvedChannel(MessengerChannel(conn.external_account_id, _env(conn, "token_env"), get_settings().meta_graph_api_version),
                                   True, "live Messenger (connection authorized)")
        if channel == TWILIO_SMS and _env(conn, "token_env") and _env(conn, "account_sid_env"):
            return ResolvedChannel(TwilioSmsChannel(_env(conn, "account_sid_env"), _env(conn, "token_env"), conn.external_account_id),
                                   True, "live Twilio SMS (connection authorized)")
    if not live_switch:
        why = "DF_LIVE_CUSTOMER_MESSAGING_ENABLED is false"
    elif conn is None or conn.status != "connected":
        why = f"no authorized {PROVIDER_NAME.get(channel, channel)} connection for this dealership"
    else:
        why = f"credentials for {PROVIDER_NAME.get(channel, channel)} are not set (config token_env / account_sid_env must name DF_* variables)"
    return ResolvedChannel(SandboxCustomerChannel(db, channel, failure_mode), False, f"sandbox: {why}")
