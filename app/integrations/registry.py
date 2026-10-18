"""Resolve the adapter to use for a capability.

A live adapter is returned only when (1) LIVE_INTEGRATIONS_ENABLED=true, (2) a human recorded
an unrevoked LiveAuthorization for the scope, and (3) credentials are configured. Otherwise the
sandbox adapter is returned and every result is marked SIMULATED.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations import sandbox
from app.integrations.base import FAILED, SendResult
from app.models import IntegrationConnection
from app.security.crypto import CredentialEncryptionError, decrypt_json
from app.services.controls import live_authorized

WEBSITE_CHAT = "WEBSITE_CHAT"
META_MESSENGER = "META_MESSENGER"
TWILIO_SMS = "TWILIO_SMS"
ADF_LEAD = "ADF_LEAD"
MARKETPLACE_ASSISTED = "MARKETPLACE_ASSISTED"

CUSTOMER_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS, ADF_LEAD, MARKETPLACE_ASSISTED)
# Channels where the platform itself can deliver a reply. Others are staff-sent outside the system.
SENDABLE_CHANNELS = (WEBSITE_CHAT, META_MESSENGER, TWILIO_SMS)

SCOPE_OUTREACH_EMAIL = "outreach.email"
SCOPE_OUTREACH_INBOX = "outreach.inbox"
SCOPE_PLACES = "outreach.discovery.google_places"
SCOPE_CALENDAR = "calendar.google"
SCOPE_VIDEO = "video.zoom"
SCOPE_NOTIFY_EMAIL = "notifications.email"
SCOPE_LLM = "llm.anthropic"

ALL_PLATFORM_SCOPES = (SCOPE_OUTREACH_EMAIL, SCOPE_OUTREACH_INBOX, SCOPE_PLACES, SCOPE_CALENDAR,
                       SCOPE_VIDEO, SCOPE_NOTIFY_EMAIL, SCOPE_LLM)


def customer_scope(provider: str) -> str:
    return f"customer_messaging.{provider}"


class FirstPartyChat:
    """Website chat: the reply is stored and the dealership's own widget fetches it. Real delivery."""

    provider = WEBSITE_CHAT
    supports_send = True

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        return SendResult("SENT", provider_message_id=None)


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


def customer_channel(db: Session, connection: IntegrationConnection | None, provider: str,
                     dealership_id: int | None = None) -> ResolvedChannel:
    dealership_id = dealership_id if dealership_id is not None else (connection.dealership_id if connection else None)
    if provider == WEBSITE_CHAT:
        # First-party, but replies still reach real website visitors → same live authorization rule as other channels.
        if live_authorized(db, customer_scope(WEBSITE_CHAT), dealership_id):
            return ResolvedChannel(FirstPartyChat(), True, "first-party website chat (live authorized)")
        return ResolvedChannel(sandbox.SandboxCustomerChannel(db, provider), False,
                               "sandbox: website chat replies not live-authorized for this dealership")
    if provider in (MARKETPLACE_ASSISTED, ADF_LEAD):
        return ResolvedChannel(ManualOnlyChannel(provider), False, "manual channel")
    failure_mode = (connection.config or {}).get("sandbox_failure_mode") if connection else None
    if connection and live_authorized(db, customer_scope(provider), connection.dealership_id):
        try:
            creds = decrypt_json(connection.encrypted_credentials)
        except CredentialEncryptionError:
            creds = {}
        settings = get_settings()
        if provider == META_MESSENGER and creds.get("page_access_token"):
            from app.integrations.meta_messenger import MessengerChannel

            return ResolvedChannel(MessengerChannel(connection.external_account_id, creds["page_access_token"],
                                                    settings.meta_graph_version), True, "live Messenger")
        if provider == TWILIO_SMS and creds.get("account_sid") and creds.get("auth_token"):
            from app.integrations.twilio_sms import TwilioSmsChannel

            return ResolvedChannel(TwilioSmsChannel(creds["account_sid"], creds["auth_token"],
                                                    connection.external_account_id), True, "live Twilio")
    return ResolvedChannel(sandbox.SandboxCustomerChannel(db, provider, failure_mode), False,
                           "sandbox (live not authorized or credentials missing)")


def _platform_failure_mode(db: Session, provider: str) -> str | None:
    from sqlalchemy import select

    conn = db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.provider == provider, IntegrationConnection.dealership_id.is_(None)))
    return (conn.config or {}).get("sandbox_failure_mode") if conn else None


def outreach_email_sender(db: Session):
    s = get_settings()
    if live_authorized(db, SCOPE_OUTREACH_EMAIL) and s.outreach_smtp_host and s.outreach_sender_email:
        from app.integrations.email_smtp import SmtpEmailSender

        return SmtpEmailSender(s.outreach_smtp_host, s.outreach_smtp_port, s.outreach_smtp_username,
                               s.outreach_smtp_password), True
    return sandbox.SandboxEmailSender(db, _platform_failure_mode(db, "OUTREACH_EMAIL")), False


def outreach_inbox(db: Session):
    s = get_settings()
    if live_authorized(db, SCOPE_OUTREACH_INBOX) and s.outreach_imap_host and s.outreach_imap_username:
        from app.integrations.email_smtp import ImapInboxReader

        return ImapInboxReader(s.outreach_imap_host, s.outreach_imap_port, s.outreach_imap_username,
                               s.outreach_imap_password or "", s.outreach_imap_folder), True
    return sandbox.SandboxInboxReader(db, _platform_failure_mode(db, "OUTREACH_INBOX")), False


def notification_email_sender(db: Session):
    s = get_settings()
    if live_authorized(db, SCOPE_NOTIFY_EMAIL) and s.smtp_host and s.smtp_from:
        from app.integrations.email_smtp import SmtpEmailSender

        return SmtpEmailSender(s.smtp_host, s.smtp_port, s.smtp_username, s.smtp_password, s.smtp_use_tls), True
    return sandbox.SandboxEmailSender(db, _platform_failure_mode(db, "NOTIFY_EMAIL")), False


def calendar(db: Session, calendar_key: str = "operator"):
    s = get_settings()
    if (live_authorized(db, SCOPE_CALENDAR) and s.google_oauth_client_id and s.google_oauth_refresh_token
            and s.google_calendar_id and calendar_key == "operator"):
        from app.integrations.google_calendar import GoogleCalendar

        return GoogleCalendar(s.google_oauth_client_id, s.google_oauth_client_secret or "",
                              s.google_oauth_refresh_token, s.google_calendar_id)
    return sandbox.SandboxCalendar(db, calendar_key, _platform_failure_mode(db, "CALENDAR"))


def dealership_calendar(db: Session, dealership_id: int):
    """Per-dealership test-drive calendar. Returns None when no calendar is connected."""
    from sqlalchemy import select

    conn = db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.dealership_id == dealership_id, IntegrationConnection.provider == "GOOGLE_CALENDAR"))
    if conn is None or conn.status == "DISABLED":
        return None
    if live_authorized(db, SCOPE_CALENDAR, dealership_id):
        try:
            creds = decrypt_json(conn.encrypted_credentials)
        except CredentialEncryptionError:
            creds = {}
        s = get_settings()
        if creds.get("refresh_token") and s.google_oauth_client_id:
            from app.integrations.google_calendar import GoogleCalendar

            return GoogleCalendar(s.google_oauth_client_id, s.google_oauth_client_secret or "",
                                  creds["refresh_token"], conn.external_account_id)
    return sandbox.SandboxCalendar(db, f"dealership:{dealership_id}", (conn.config or {}).get("sandbox_failure_mode"))


def video(db: Session):
    s = get_settings()
    if live_authorized(db, SCOPE_VIDEO) and s.zoom_account_id and s.zoom_client_id and s.zoom_client_secret:
        from app.integrations.zoom import ZoomMeetings

        return ZoomMeetings(s.zoom_account_id, s.zoom_client_id, s.zoom_client_secret)
    return sandbox.SandboxVideo(db, _platform_failure_mode(db, "VIDEO"))


def prospect_source(db: Session):
    s = get_settings()
    if live_authorized(db, SCOPE_PLACES) and s.google_places_api_key:
        from app.integrations.google_places import GooglePlacesSource

        return GooglePlacesSource(s.google_places_api_key)
    return sandbox.SandboxProspectSource()


def llm(db: Session):
    s = get_settings()
    if s.llm_enabled and s.anthropic_api_key and live_authorized(db, SCOPE_LLM):
        try:
            from app.integrations.llm import AnthropicLLM

            return AnthropicLLM(s.anthropic_api_key, s.llm_model)
        except ImportError:
            return None
    return None


def integration_status(db: Session) -> list[dict]:
    """Truthful status for the admin workspace (never includes secrets)."""
    s = get_settings()
    rows = [
        ("Website chat (first-party)", True, True, "Implemented and tested; no third-party approval needed."),
        ("ADF/XML lead intake", True, True, "HTTP intake implemented and tested; replies are sent by staff."),
        ("Facebook Marketplace", False, False, "No official API for Marketplace chats. Human-assisted workflow only."),
        ("Meta Messenger (Page)", bool(s.meta_app_secret and s.meta_verify_token), None,
         "Needs Meta App Review (pages_messaging), Business Verification and per-Page tokens."),
        ("Twilio SMS", None, None, "Needs per-dealership Twilio credentials + A2P 10DLC registration."),
        ("Outreach email (SMTP)", bool(s.outreach_smtp_host and s.outreach_sender_email and s.outreach_postal_address),
         live_authorized(db, SCOPE_OUTREACH_EMAIL), "Operator mailbox; CAN-SPAM postal address required."),
        ("Outreach inbox (IMAP)", bool(s.outreach_imap_host), live_authorized(db, SCOPE_OUTREACH_INBOX), ""),
        ("Google Places discovery", bool(s.google_places_api_key), live_authorized(db, SCOPE_PLACES),
         "Only place_id stored long-term; other content expires in 30 days."),
        ("Google Calendar", bool(s.google_oauth_client_id and s.google_oauth_refresh_token), live_authorized(db, SCOPE_CALENDAR), ""),
        ("Zoom", bool(s.zoom_account_id and s.zoom_client_id), live_authorized(db, SCOPE_VIDEO), ""),
        ("Notification email (SMTP)", bool(s.smtp_host and s.smtp_from), live_authorized(db, SCOPE_NOTIFY_EMAIL), ""),
        ("Claude LLM rephrasing", bool(s.llm_enabled and s.anthropic_api_key), live_authorized(db, SCOPE_LLM),
         "Optional; deterministic grounded engine works without it."),
    ]
    return [{"name": n, "configured": c, "live_authorized": a, "note": note,
             "master_switch": s.live_integrations_enabled} for n, c, a, note in rows]
