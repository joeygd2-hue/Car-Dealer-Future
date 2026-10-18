"""Outbound email providers.

`sandbox` (default) stores the message in `outbound_emails` with status
`sandboxed` and never contacts a mail server. `smtp` delivers through an
authorized SMTP relay. Callers decide whether live delivery is permitted
(`allow_live`); when it is not, the sandbox is used regardless of config.
"""
from __future__ import annotations

import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.core import OutboundEmail


class EmailDeliveryError(Exception):
    pass


def provider_name(allow_live: bool) -> str:
    settings = get_settings()
    if not allow_live or settings.email_provider == "sandbox":
        return "sandbox"
    return settings.email_provider


def _smtp_send(msg: EmailMessage) -> None:
    s = get_settings()
    if not s.smtp_host:
        raise EmailDeliveryError("SMTP_HOST is not configured")
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        if s.smtp_use_tls:
            server.starttls(context=ctx)
    try:
        if s.smtp_username:
            server.login(s.smtp_username, s.smtp_password)
        server.send_message(msg)
    finally:
        server.quit()


def send_email(
    db: Session,
    *,
    purpose: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    from_address: str | None = None,
    reply_to: str | None = None,
    extra_headers: dict | None = None,
    idempotency_key: str | None = None,
    client_id: int | None = None,
    allow_live: bool = False,
) -> OutboundEmail:
    """Send (or sandbox) an email exactly once per idempotency key."""
    if idempotency_key:
        existing = db.scalar(select(OutboundEmail).where(OutboundEmail.idempotency_key == idempotency_key))
        if existing and existing.status in ("sent", "sandboxed"):
            return existing
    s = get_settings()
    provider = provider_name(allow_live)
    sender = from_address or s.outreach_from_address or f"no-reply@{s.base_url.split('//')[-1].split('/')[0]}"
    domain = sender.split("@")[-1] or "localhost"
    message_id = make_msgid(idstring=uuid.uuid4().hex[:12], domain=domain)
    headers = dict(extra_headers or {})
    headers["Message-ID"] = message_id
    if reply_to:
        headers["Reply-To"] = reply_to

    row = OutboundEmail(
        purpose=purpose,
        to_address=to,
        from_address=sender,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        headers=headers,
        provider=provider,
        status="pending",
        provider_message_id=message_id,
        idempotency_key=idempotency_key,
        client_id=client_id,
    )
    db.add(row)
    db.flush()

    if provider == "sandbox":
        row.status = "sandboxed"
        row.sent_at = utcnow()
        return row

    msg = EmailMessage()
    msg["From"] = formataddr((s.company_name, sender))
    msg["To"] = to
    msg["Subject"] = subject
    for k, v in headers.items():
        msg[k] = v
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    try:
        if provider == "smtp":
            _smtp_send(msg)
        else:
            raise EmailDeliveryError(f"Unsupported email provider '{provider}'")
        row.status = "sent"
        row.sent_at = utcnow()
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.error = f"{type(exc).__name__}: {exc}"[:2000]
        raise EmailDeliveryError(row.error) from exc
    return row
