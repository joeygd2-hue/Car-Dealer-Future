"""Sandbox providers: simulated mailbox, calendar and prospect discovery.
Nothing here contacts a real person or service. Simulated prospects use
reserved ``.example`` domains (RFC 2606)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import new_id, sha256, utcnow
from app.models import Meeting, SandboxMail
from app.outreach.providers.base import (
    CalendarProvider, Candidate, DiscoveryProvider, EmailProvider, InboundEmail, OutboundEmail, ProviderError, SendResult, Slot,
)


class SandboxEmailProvider(EmailProvider):
    live = False
    name = "sandbox"

    def __init__(self, db: Session, config: dict | None = None):
        self.db = db
        self.config = config or {}

    def send(self, email: OutboundEmail) -> SendResult:
        if self.config.get("fail_sends"):
            raise ProviderError("Simulated send failure", retryable=True)
        existing = self.db.scalar(select(SandboxMail).where(SandboxMail.message_id == f"<{email.idempotency_key}@sandbox>"))
        if existing:  # idempotent
            return SendResult(existing.message_id, existing.thread_id)
        thread = email.thread_id or new_id("thr")
        m = SandboxMail(direction="outbound", from_address=email.from_address, to_address=email.to, subject=email.subject,
                        body=email.body, thread_id=thread, message_id=f"<{email.idempotency_key}@sandbox>",
                        in_reply_to=email.in_reply_to)
        self.db.add(m)
        self.db.flush()
        return SendResult(m.message_id, thread)

    def fetch_inbound(self, since: datetime | None, limit: int = 50) -> list[InboundEmail]:
        rows = self.db.scalars(select(SandboxMail).where(SandboxMail.direction == "inbound", SandboxMail.consumed.is_(False))
                               .order_by(SandboxMail.created_at).limit(limit)).all()
        out = []
        for r in rows:
            r.consumed = True
            out.append(InboundEmail(provider_message_id=r.message_id, thread_id=r.thread_id, from_address=r.from_address,
                                    to_address=r.to_address, subject=r.subject, body=r.body, received_at=r.created_at,
                                    in_reply_to=r.in_reply_to))
        self.db.flush()
        return out

    def health_check(self):
        return ("down", "Simulated outage") if self.config.get("fail_sends") else ("ok", "sandbox")


def inject_reply(db: Session, *, thread_id: str, from_address: str, to_address: str, subject: str, body: str,
                 in_reply_to: str | None = None) -> SandboxMail:
    m = SandboxMail(direction="inbound", from_address=from_address, to_address=to_address, subject=subject, body=body,
                    thread_id=thread_id, message_id=f"<{new_id('in')}@sandbox>", in_reply_to=in_reply_to)
    db.add(m)
    db.flush()
    return m


class SandboxCalendarProvider(CalendarProvider):
    live = False
    name = "sandbox"

    def __init__(self, db: Session, config: dict | None = None):
        self.db = db
        self.config = config or {}

    def busy(self, start: datetime, end: datetime) -> list[Slot]:
        if self.config.get("fail"):
            raise ProviderError("Simulated calendar outage", retryable=True)
        rows = self.db.scalars(select(Meeting).where(Meeting.status == "booked", Meeting.start_at < end, Meeting.end_at > start)).all()
        return [Slot(m.start_at, m.end_at) for m in rows]

    def create_event(self, *, start, end, attendee_email, summary, description, idempotency_key):
        if self.config.get("fail"):
            raise ProviderError("Simulated calendar outage", retryable=True)
        eid = "sbx" + sha256(idempotency_key)[:20]
        return eid, f"https://meet.sandbox.invalid/{eid[:10]}"


SIMULATED_DEALERS = [
    ("Brazos Valley Auto Sales", "Sugar Land", "77478"), ("Fort Bend Pre-Owned", "Sugar Land", "77479"),
    ("Riverstone Motors", "Sugar Land", "77479"), ("First Colony Car Co", "Sugar Land", "77478"),
    ("Stafford Auto Outlet", "Stafford", "77477"), ("Missouri City Motors", "Missouri City", "77459"),
    ("Richmond Road Autos", "Richmond", "77469"), ("Alief Auto Plaza", "Houston", "77072"),
    ("Westpark Used Cars", "Houston", "77042"), ("Rosenberg Auto Mart", "Rosenberg", "77471"),
    ("Sugar Land Toyota", "Sugar Land", "77479"),  # franchise -> should be disqualified
    ("Bellaire Car Exchange", "Bellaire", "77401"),
]


class SandboxDiscoveryProvider(DiscoveryProvider):
    live = False
    name = "sandbox"
    estimated_cost_per_call = 0.0

    def __init__(self, db: Session | None = None, config: dict | None = None):
        self.config = config or {}

    def search(self, query: str, limit: int = 20) -> list[Candidate]:
        out = []
        for name, city, zip_ in SIMULATED_DEALERS[:limit]:
            slug = "".join(ch for ch in name.lower() if ch.isalnum())
            out.append(Candidate(business_name=name, source="sandbox", source_ref=f"sim-{slug}", website=f"https://{slug}.example",
                                 phone="(281) 555-01" + str(len(slug) % 90 + 10), address=f"{100 + len(slug)} Main St",
                                 city=city, state="TX", postal_code=zip_, types=["car_dealer"], business_status="OPERATIONAL",
                                 simulated=True))
        return out


def sandbox_site_html(domain: str) -> dict[str, str] | None:
    """Fixture websites for simulated prospects (used by the enrichment fetcher)."""
    if not domain.endswith(".example"):
        return None
    name = domain.split(".")[0]
    spanish = len(name) % 2 == 0
    home = f"""<html><head><title>{name.title()} | Quality Used Cars</title>
<meta name="description" content="Family-owned used car dealership."></head><body>
<a href="/inventory">View Inventory</a> <a href="https://www.facebook.com/{name}">Facebook</a>
{'<p>Se habla español</p>' if spanish else ''}<p>Call us: (281) 555-0100</p></body></html>"""
    contact = f"""<html><body><h1>Contact</h1><p>Email: <a href="mailto:sales@{domain}">sales@{domain}</a></p></body></html>"""
    return {"/": home, "/contact": contact}
