"""Email to client dealerships (monthly reports, lead summaries). Transactional, not outreach.

Delivery is real only when ALL hold:
  * DF_CLIENT_EMAIL_SENDING_ENABLED=true,
  * the configured email provider is a real one (not ``mock``),
  * the client has a ``report_email`` integration connection marked ``connected``.
Otherwise nothing leaves the system: with the mock provider the message is recorded as SIMULATED;
with a real provider but no authorization it is recorded as ``not_sent``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import log_integration, track_integration
from ..config import get_settings
from ..integrations.email import OutgoingEmail, get_email_provider
from ..models import Client, IntegrationConnection


def delivery_allowed(db: Session, client: Client) -> tuple[bool, str]:
    s = get_settings()
    if not s.client_email_sending_enabled:
        return False, "DF_CLIENT_EMAIL_SENDING_ENABLED is false"
    if s.email_provider == "mock":
        return False, "email provider is the SIMULATED mock"
    conn = db.scalar(select(IntegrationConnection).where(
        IntegrationConnection.client_id == client.id, IntegrationConnection.kind == "report_email",
        IntegrationConnection.status == "connected"))
    if conn is None:
        return False, "client has no authorized report_email connection"
    return True, ""


def send(db: Session, client: Client, *, to: str, subject: str, body: str, key: str) -> str:
    """Return 'sent', 'simulated' or 'not_sent: <reason>'."""
    s = get_settings()
    allowed, why = delivery_allowed(db, client)
    msg = OutgoingEmail(to=to, subject=subject, body=body, from_name=s.business_name or s.sender_name, from_email=s.sender_email,
                        idempotency_key=key)
    if allowed:
        provider = get_email_provider()
        with track_integration(db, provider.name, "client_email", simulated=False):
            provider.send(msg)
        return "sent"
    if s.email_provider == "mock":
        provider = get_email_provider()
        with track_integration(db, "mock", "client_email", simulated=True):
            provider.send(msg)
        return "simulated"
    log_integration(db, s.email_provider, "client_email", "skipped", detail=why)
    return f"not_sent: {why}"
