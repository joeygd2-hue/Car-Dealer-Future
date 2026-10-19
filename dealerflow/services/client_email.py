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


def send(db: Session, client: Client, *, to: str, subject: str, body: str, key: str, ledger: bool = True) -> str:
    """Return 'sent', 'simulated' or 'not_sent: <reason>'.

    Every attempt passes the send gate (platform emergency stop; pilot allowlist for a real delivery) and, unless the
    caller keeps its own ledger row (``ledger=False``), is recorded in ``send_attempts``.
    """
    from ..integrations.customer_channels import SendResult
    from ..integrations.errors import CredentialError, IntegrationError, PermanentError, RateLimitedError
    from .customer import send_gate

    s = get_settings()
    allowed, why = delivery_allowed(db, client)
    msg = OutgoingEmail(to=to, subject=subject, body=body, from_name=s.business_name or s.sender_name, from_email=s.sender_email,
                        idempotency_key=key)
    if not allowed and s.email_provider != "mock":
        log_integration(db, s.email_provider, "client_email", "skipped", detail=why)
        return f"not_sent: {why}"
    gate = None
    if ledger:
        gate = send_gate.open_attempt(db, kind="CLIENT_EMAIL", ref_id=client.id, channel="EMAIL", recipient=to, live=allowed,
                                      client_id=client.id)
        if not gate.allowed:
            log_integration(db, s.email_provider, "client_email", "blocked", detail="; ".join(gate.reasons))
            return "not_sent: " + "; ".join(gate.reasons)
    elif allowed:
        from . import pilot

        blocker = pilot.live_send_blocker(db, "EMAIL", to)
        if blocker:
            log_integration(db, s.email_provider, "client_email", "blocked", detail=blocker)
            return f"not_sent: {blocker}"
    provider = get_email_provider()
    try:
        with track_integration(db, provider.name if allowed else "mock", "client_email", simulated=not allowed):
            res = provider.send(msg)
    except IntegrationError as exc:
        if gate:
            send_gate.close_attempt(db, gate.attempt, SendResult("FAILED", error=str(exc), uncertain=not isinstance(exc, (CredentialError, PermanentError, RateLimitedError))))
        raise
    if gate:
        send_gate.close_attempt(db, gate.attempt, SendResult("SENT" if allowed else "SIMULATED", provider_message_id=res.provider_message_id))
    return "sent" if allowed else "simulated"
