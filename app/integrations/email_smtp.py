"""SMTP sending and IMAP reading for mailboxes the operator/dealership controls.

Bulk ESPs (Postmark, SendGrid, Resend) prohibit cold outreach, so outreach uses the operator's
own Google Workspace / Microsoft 365 mailbox via SMTP (app password or SMTP AUTH) and IMAP.
"""

from __future__ import annotations

import email
import imaplib
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime

from app.db import utcnow
from app.integrations.base import FAILED, SENT, InboundEmail, IntegrationError, SendResult


class SmtpEmailSender:
    name = "smtp"

    def __init__(self, host: str, port: int, username: str | None, password: str | None, use_tls: bool = True):
        self.host, self.port, self.username, self.password, self.use_tls = host, port, username, password, use_tls

    def send(self, *, to, subject, body, from_addr, from_name=None, headers=None, message_id=None, in_reply_to=None) -> SendResult:
        msg = EmailMessage()
        msg["From"] = formataddr((from_name or "", from_addr))
        msg["To"] = to
        msg["Subject"] = subject
        domain = from_addr.split("@")[-1] if "@" in from_addr else None
        msg["Message-ID"] = message_id or make_msgid(domain=domain)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(body)
        try:
            smtp = smtplib.SMTP(self.host, self.port, timeout=20)
        except OSError as exc:  # could not connect: nothing was sent
            return SendResult(FAILED, error=f"SMTP connect error: {exc}", retryable=True)
        try:
            with smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                refused = smtp.send_message(msg)
        except (smtplib.SMTPServerDisconnected, socket.timeout) as exc:
            # The connection dropped during the transaction; the server may already have accepted the message.
            return SendResult(FAILED, error=f"delivery uncertain: {exc}", uncertain=True)
        except smtplib.SMTPRecipientsRefused as exc:
            return SendResult(FAILED, error=f"recipient refused: {exc.recipients}", retryable=False)
        except smtplib.SMTPAuthenticationError as exc:
            return SendResult(FAILED, error=f"SMTP auth failed: {exc.smtp_code}", retryable=False)
        except smtplib.SMTPResponseException as exc:  # the server explicitly refused: nothing was accepted
            return SendResult(FAILED, error=f"SMTP {exc.smtp_code}: {exc.smtp_error!r}", retryable=400 <= exc.smtp_code < 500)
        except (smtplib.SMTPException, OSError) as exc:  # connection broke mid-transaction: outcome unknown
            return SendResult(FAILED, error=f"delivery uncertain: {exc}", uncertain=True)
        if refused:
            return SendResult(FAILED, error=f"recipient refused: {refused}", retryable=False)
        return SendResult(SENT, provider_message_id=msg["Message-ID"])


class ImapInboxReader:
    name = "imap"

    def __init__(self, host: str, port: int, username: str, password: str, folder: str = "INBOX"):
        self.host, self.port, self.username, self.password, self.folder = host, port, username, password, folder

    def fetch_new(self, cursor: dict) -> tuple[list[InboundEmail], dict]:
        last_uid = int(cursor.get("imap_last_uid", 0))
        try:
            conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
            conn.login(self.username, self.password)
            conn.select(self.folder, readonly=True)
            typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid]
            out: list[InboundEmail] = []
            for uid in uids[:200]:
                typ, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                out.append(parse_rfc822(msg_data[0][1]))
                last_uid = max(last_uid, uid)
            conn.logout()
        except (imaplib.IMAP4.error, OSError) as exc:
            raise IntegrationError(f"IMAP error: {exc}", retryable=True) from exc
        return out, {**cursor, "imap_last_uid": last_uid}


def parse_rfc822(raw: bytes) -> InboundEmail:
    msg = email.message_from_bytes(raw)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                break
    else:
        body = (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "replace")
    try:
        received = parsedate_to_datetime(msg.get("Date")).replace(tzinfo=None) if msg.get("Date") else utcnow()
    except (TypeError, ValueError):
        received = utcnow()
    refs = (msg.get("References") or "").split()
    return InboundEmail(
        message_id=(msg.get("Message-ID") or "").strip() or f"<no-id-{hash(raw)}@local>",
        from_addr=parseaddr(msg.get("From", ""))[1].lower(),
        subject=str(msg.get("Subject", "")),
        body=body,
        in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
        references=refs,
        received_at=received,
        simulated=False,
    )
