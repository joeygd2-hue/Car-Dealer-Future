"""Inbound email sources.

* sandbox  — messages injected through the operator API / tests (`sandbox_inbox`).
* imap     — polls an authorized mailbox over IMAPS (stdlib imaplib). Progress is
             checkpointed by UID in the integration connection settings so a
             restart never reprocesses or skips mail.
             STATUS: implemented but NOT verified against a real mailbox here.
* webhook  — POST /api/v1/webhooks/inbound-email with an HMAC-SHA256 signature
             (`X-Signature: sha256=<hex>` over the raw body, secret
             INBOUND_WEBHOOK_SECRET) for providers that forward inbound mail.
"""
from __future__ import annotations

import email
import hashlib
import hmac
import imaplib
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.core import IntegrationConnection


@dataclass
class RawInbound:
    provider_message_id: str
    from_address: str
    subject: str | None
    body: str
    in_reply_to: str | None = None
    to_addresses: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)


sandbox_inbox: list[RawInbound] = []


def verify_webhook_signature(raw: bytes, header: str | None) -> bool:
    secret = get_settings().inbound_webhook_secret
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def _text_of(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in (part.get("Content-Disposition") or ""):
                return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    return (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "replace")


def parse_rfc822(raw: bytes) -> RawInbound:
    m = email.message_from_bytes(raw)
    subject = str(make_header(decode_header(m.get("Subject", "")))) if m.get("Subject") else None
    mid = (m.get("Message-ID") or "").strip() or "sha256:" + hashlib.sha256(raw).hexdigest()
    return RawInbound(
        provider_message_id=mid, from_address=parseaddr(m.get("From", ""))[1], subject=subject, body=_text_of(m),
        in_reply_to=(m.get("In-Reply-To") or "").strip() or None,
        to_addresses=[a for _, a in getaddresses(m.get_all("To", []) + m.get_all("Cc", []))],
        headers={k: v for k, v in m.items() if k.lower() in ("auto-submitted", "x-autoreply", "precedence", "list-id")},
    )


def _imap_connection(db: Session) -> IntegrationConnection:
    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id.is_(None), IntegrationConnection.kind == "outreach_inbox", IntegrationConnection.provider == "imap"))
    if conn is None:
        conn = IntegrationConnection(client_id=None, kind="outreach_inbox", provider="imap", status="not_connected", settings={})
        db.add(conn)
        db.flush()
    return conn


def fetch_new(db: Session, live: bool, max_messages: int = 50) -> tuple[str, list[RawInbound]]:
    s = get_settings()
    if not live or s.inbox_provider == "sandbox":
        items = sandbox_inbox[:max_messages]
        del sandbox_inbox[: len(items)]
        return "sandbox", items
    if s.inbox_provider != "imap":
        raise RuntimeError(f"Unsupported inbox provider {s.inbox_provider}")
    conn = _imap_connection(db)
    last_uid = int(conn.settings.get("last_uid", 0))
    out: list[RawInbound] = []
    client = imaplib.IMAP4_SSL(s.imap_host, s.imap_port)
    try:
        client.login(s.imap_username, s.imap_password)
        client.select(s.imap_folder, readonly=True)
        typ, data = client.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid][:max_messages]
        for uid in uids:
            typ, msg_data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ == "OK" and msg_data and isinstance(msg_data[0], tuple):
                out.append(parse_rfc822(msg_data[0][1]))
            last_uid = uid
        conn.settings = {**conn.settings, "last_uid": last_uid}
        conn.status = "connected"
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
    return "imap", out
