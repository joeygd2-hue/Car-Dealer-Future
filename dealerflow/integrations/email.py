"""Email providers: send outreach and fetch replies from an authorized inbox.

- ``MockEmailProvider`` (SIMULATED): keeps messages in memory. Nothing leaves the process.
- ``SmtpImapProvider``: any mailbox that permits SMTP submission and IMAP (e.g. Google Workspace).
- ``GmailApiProvider``: Gmail API with OAuth scopes gmail.send + gmail.readonly.
"""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import smtplib
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr, parsedate_to_datetime

from ..config import get_settings
from .errors import CredentialError, IntegrationError, NotConfiguredError
from .http import request


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    body: str
    from_name: str
    from_email: str
    unsubscribe_url: str | None = None
    unsubscribe_mailto: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    thread_id: str | None = None
    idempotency_key: str | None = None


@dataclass
class SendResult:
    provider_message_id: str
    rfc_message_id: str
    thread_id: str | None
    simulated: bool


@dataclass
class IncomingEmail:
    provider_message_id: str
    from_email: str
    subject: str
    body: str
    received_at: datetime
    rfc_message_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    thread_id: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    simulated: bool = False


def build_mime(msg: OutgoingEmail, domain: str) -> EmailMessage:
    m = EmailMessage()
    m["From"] = f"{msg.from_name} <{msg.from_email}>" if msg.from_name else msg.from_email
    m["To"] = msg.to
    m["Subject"] = msg.subject
    m["Date"] = format_datetime(datetime.now(timezone.utc))
    m["Message-ID"] = make_msgid(idstring=(msg.idempotency_key or uuid.uuid4().hex).replace(":", "."), domain=domain)
    if msg.in_reply_to:
        m["In-Reply-To"] = msg.in_reply_to
        m["References"] = msg.references or msg.in_reply_to
    unsub = []
    if msg.unsubscribe_url:
        unsub.append(f"<{msg.unsubscribe_url}>")
    if msg.unsubscribe_mailto:
        unsub.append(f"<mailto:{msg.unsubscribe_mailto}?subject=unsubscribe>")
    if unsub:
        m["List-Unsubscribe"] = ", ".join(unsub)
        if msg.unsubscribe_url:
            m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"  # RFC 8058
    m.set_content(msg.body)
    return m


def _text_body(m: email.message.Message) -> str:
    if m.is_multipart():
        for part in m.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_content() if hasattr(part, "get_content") else part.get_payload(decode=True).decode(errors="replace")
        for part in m.walk():
            if part.get_content_type() == "text/html":
                import re
                html = part.get_content()
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    try:
        return m.get_content()
    except Exception:  # noqa: BLE001
        payload = m.get_payload(decode=True)
        return payload.decode(errors="replace") if payload else ""


def parse_raw(raw: bytes, provider_message_id: str, thread_id: str | None = None) -> IncomingEmail:
    m = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        received = parsedate_to_datetime(m["Date"]).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        received = datetime.now(timezone.utc).replace(tzinfo=None)
    return IncomingEmail(
        provider_message_id=provider_message_id,
        from_email=parseaddr(str(m.get("From", "")))[1].lower(),
        subject=str(m.get("Subject", "")),
        body=_text_body(m)[:20000],
        received_at=received,
        rfc_message_id=str(m.get("Message-ID", "")) or None,
        in_reply_to=str(m.get("In-Reply-To", "")) or None,
        references=str(m.get("References", "")) or None,
        thread_id=thread_id,
        headers={k: str(v) for k, v in m.items() if k.lower() in ("auto-submitted", "x-autoreply", "precedence",
                                                                   "x-failed-recipients", "content-type")},
    )


class EmailProvider:
    name = "base"
    simulated = False

    def send(self, msg: OutgoingEmail) -> SendResult:
        raise NotImplementedError

    def fetch_inbound(self, cursor: str | None) -> tuple[list[IncomingEmail], str | None]:
        raise NotImplementedError

    def check(self) -> str:
        """Verify credentials without sending anything. Returns a short description."""
        raise NotImplementedError


class MockEmailProvider(EmailProvider):
    """SIMULATED provider. Tests and the dev UI push replies with ``inject_reply``."""

    name = "mock"
    simulated = True
    outbox: list[tuple[OutgoingEmail, SendResult]] = []
    inbox: list[IncomingEmail] = []
    fail_next: list[Exception] = []

    @classmethod
    def reset(cls) -> None:
        cls.outbox, cls.inbox, cls.fail_next = [], [], []

    def send(self, msg: OutgoingEmail) -> SendResult:
        if self.fail_next:
            raise self.fail_next.pop(0)
        rid = f"<sim-{uuid.uuid4().hex}@simulated.invalid>"
        res = SendResult(provider_message_id=f"sim-{uuid.uuid4().hex[:12]}", rfc_message_id=rid,
                         thread_id=msg.thread_id or f"simthread-{uuid.uuid4().hex[:8]}", simulated=True)
        type(self).outbox.append((msg, res))
        return res

    @classmethod
    def inject_reply(cls, from_email: str, subject: str, body: str, *, in_reply_to: str | None = None,
                     thread_id: str | None = None, headers: dict[str, str] | None = None) -> IncomingEmail:
        inc = IncomingEmail(provider_message_id=f"sim-in-{uuid.uuid4().hex[:12]}", from_email=from_email.lower(),
                            subject=subject, body=body, received_at=datetime.now(timezone.utc).replace(tzinfo=None),
                            rfc_message_id=f"<sim-in-{uuid.uuid4().hex}@simulated.invalid>", in_reply_to=in_reply_to,
                            references=in_reply_to, thread_id=thread_id, headers=headers or {}, simulated=True)
        cls.inbox.append(inc)
        return inc

    def fetch_inbound(self, cursor: str | None) -> tuple[list[IncomingEmail], str | None]:
        if self.fail_next:
            raise self.fail_next.pop(0)
        start = int(cursor or 0)
        items = type(self).inbox[start:]
        return items, str(len(type(self).inbox))

    def check(self) -> str:
        return "SIMULATED mailbox (no real email is sent or read)"


class SmtpImapProvider(EmailProvider):
    name = "smtp"

    def __init__(self) -> None:
        s = get_settings()
        if not (s.smtp_host and s.smtp_username and s.smtp_password.get_secret_value()):
            raise NotConfiguredError("SMTP is not configured (DF_SMTP_HOST / _USERNAME / _PASSWORD)")
        self.s = s

    def send(self, msg: OutgoingEmail) -> SendResult:
        s = self.s
        mime = build_mime(msg, msg.from_email.split("@")[-1])
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                refused = smtp.send_message(mime)
        except smtplib.SMTPAuthenticationError as exc:
            raise CredentialError(f"SMTP authentication failed: {exc.smtp_code}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise IntegrationError(f"SMTP send failed: {exc}") from exc
        if refused:
            raise IntegrationError(f"SMTP refused recipients: {refused}")
        return SendResult(provider_message_id=mime["Message-ID"], rfc_message_id=mime["Message-ID"],
                          thread_id=None, simulated=False)

    def _imap(self) -> imaplib.IMAP4_SSL:
        s = self.s
        if not (s.imap_host and s.imap_username and s.imap_password.get_secret_value()):
            raise NotConfiguredError("IMAP is not configured (DF_IMAP_HOST / _USERNAME / _PASSWORD)")
        try:
            conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
            conn.login(s.imap_username, s.imap_password.get_secret_value())
        except imaplib.IMAP4.error as exc:
            raise CredentialError(f"IMAP login failed: {exc}") from exc
        except OSError as exc:
            raise IntegrationError(f"IMAP connection failed: {exc}") from exc
        return conn

    def fetch_inbound(self, cursor: str | None) -> tuple[list[IncomingEmail], str | None]:
        """Read-only: messages are fetched with BODY.PEEK so their read state is untouched."""
        conn = self._imap()
        try:
            conn.select(self.s.imap_folder, readonly=True)
            last_uid = int(cursor or 0)
            typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid][:200]
            out = []
            for uid in uids:
                typ, msg_data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                if raw:
                    out.append(parse_raw(raw, f"imap-{self.s.imap_username}-{uid}"))
                last_uid = max(last_uid, uid)
            return out, str(last_uid)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def check(self) -> str:
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
            if self.s.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            try:
                smtp.login(self.s.smtp_username, self.s.smtp_password.get_secret_value())
            except smtplib.SMTPAuthenticationError as exc:
                raise CredentialError("SMTP authentication failed") from exc
        if self.s.imap_host:
            self._imap().logout()
        return f"SMTP {self.s.smtp_host} login ok" + (f"; IMAP {self.s.imap_host} login ok" if self.s.imap_host else "")


class GmailApiProvider(EmailProvider):
    name = "gmail"
    BASE = "https://gmail.googleapis.com/gmail/v1/users"

    def __init__(self) -> None:
        from .google_auth import access_token

        self._token = access_token
        self.user = get_settings().gmail_user

    def _h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def send(self, msg: OutgoingEmail) -> SendResult:
        mime = build_mime(msg, msg.from_email.split("@")[-1])
        body: dict = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if msg.thread_id:
            body["threadId"] = msg.thread_id
        resp = request("POST", f"{self.BASE}/{self.user}/messages/send", provider="gmail", headers=self._h(),
                       json=body, retries=1, rate=(5, 1.0))
        data = resp.json()
        return SendResult(provider_message_id=data["id"], rfc_message_id=mime["Message-ID"],
                          thread_id=data.get("threadId"), simulated=False)

    def fetch_inbound(self, cursor: str | None) -> tuple[list[IncomingEmail], str | None]:
        """Lists inbox messages received after ``cursor`` (epoch seconds)."""
        since = int(cursor or 0) or int(datetime.now(timezone.utc).timestamp()) - 7 * 86400
        q = f"in:inbox after:{since}"
        resp = request("GET", f"{self.BASE}/{self.user}/messages", provider="gmail", headers=self._h(),
                       params={"q": q, "maxResults": 100})
        ids = [m["id"] for m in resp.json().get("messages", [])]
        out, newest = [], since
        for mid in ids:
            r = request("GET", f"{self.BASE}/{self.user}/messages/{mid}", provider="gmail", headers=self._h(),
                        params={"format": "raw"})
            d = r.json()
            raw = base64.urlsafe_b64decode(d["raw"] + "==")
            inc = parse_raw(raw, d["id"], d.get("threadId"))
            out.append(inc)
            newest = max(newest, int(int(d.get("internalDate", "0")) / 1000))
        return out, str(newest)

    def check(self) -> str:
        r = request("GET", f"{self.BASE}/{self.user}/profile", provider="gmail", headers=self._h(), retries=1)
        return f"Gmail API ok for {r.json().get('emailAddress')}"


def get_email_provider() -> EmailProvider:
    kind = get_settings().email_provider
    if kind == "mock":
        return MockEmailProvider()
    if kind == "smtp":
        return SmtpImapProvider()
    if kind == "gmail":
        return GmailApiProvider()
    raise NotConfiguredError(f"Unknown email provider {kind!r}")
