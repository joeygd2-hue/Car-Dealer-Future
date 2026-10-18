"""Sandbox adapters. Default for every provider until live operation is explicitly authorized.

They persist simulated traffic in ``sandbox_records`` and can be told to fail via
``failure_mode`` ("retryable" | "permanent") to exercise error handling.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.integrations.base import (
    FAILED,
    SIMULATED,
    BusyInterval,
    InboundEmail,
    IntegrationError,
    ProspectCandidate,
    SendResult,
    VideoMeeting,
)
from app.models.sandbox import SandboxRecord


def _fail(failure_mode: str | None) -> SendResult | None:
    if failure_mode == "retryable":
        return SendResult(FAILED, error="sandbox: simulated transient provider error (HTTP 503)", retryable=True)
    if failure_mode == "permanent":
        return SendResult(FAILED, error="sandbox: simulated permanent provider error (HTTP 400)", retryable=False)
    if failure_mode == "uncertain":
        return SendResult(FAILED, error="sandbox: simulated read timeout after request was sent", uncertain=True)
    return None


class SandboxCustomerChannel:
    supports_send = True

    def __init__(self, db: Session, provider: str, failure_mode: str | None = None):
        self.db = db
        self.provider = provider
        self.failure_mode = failure_mode

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        failed = _fail(self.failure_mode)
        if failed:
            return failed
        mid = f"sandbox-{uuid.uuid4().hex[:12]}"
        self.db.add(SandboxRecord(kind="CHANNEL_OUT", payload={
            "provider": self.provider, "recipient": recipient_id, "text": text, "id": mid, "human_agent": human_agent}))
        return SendResult(SIMULATED, provider_message_id=mid)


class SandboxEmailSender:
    name = "sandbox_email"

    def __init__(self, db: Session, failure_mode: str | None = None):
        self.db = db
        self.failure_mode = failure_mode

    def send(self, *, to, subject, body, from_addr, from_name=None, headers=None, message_id=None, in_reply_to=None) -> SendResult:
        failed = _fail(self.failure_mode)
        if failed:
            return failed
        mid = message_id or f"<sandbox-{uuid.uuid4().hex}@sandbox.invalid>"
        self.db.add(SandboxRecord(kind="EMAIL_OUT", payload={
            "to": to, "subject": subject, "body": body, "from": from_addr, "from_name": from_name,
            "headers": headers or {}, "message_id": mid, "in_reply_to": in_reply_to}))
        return SendResult(SIMULATED, provider_message_id=mid)


def add_simulated_inbound_email(db: Session, *, from_addr: str, subject: str, body: str,
                                in_reply_to: str | None = None) -> SandboxRecord:
    """Used by tests and the admin 'simulate reply' tool to inject a prospect reply."""
    rec = SandboxRecord(kind="EMAIL_IN", payload={
        "message_id": f"<sim-{uuid.uuid4().hex}@sandbox.invalid>", "from": from_addr, "subject": subject,
        "body": body, "in_reply_to": in_reply_to, "references": [in_reply_to] if in_reply_to else []})
    db.add(rec)
    db.flush()
    return rec


class SandboxInboxReader:
    name = "sandbox_inbox"

    def __init__(self, db: Session, failure_mode: str | None = None):
        self.db = db
        self.failure_mode = failure_mode

    def fetch_new(self, cursor: dict) -> tuple[list[InboundEmail], dict]:
        if self.failure_mode:
            raise IntegrationError("sandbox: simulated inbox connection failure", retryable=self.failure_mode == "retryable")
        last_id = int(cursor.get("sandbox_last_id", 0))
        rows = list(self.db.scalars(
            select(SandboxRecord).where(SandboxRecord.kind == "EMAIL_IN", SandboxRecord.id > last_id).order_by(SandboxRecord.id)))
        out = []
        for r in rows:
            p = r.payload
            out.append(InboundEmail(message_id=p["message_id"], from_addr=p["from"], subject=p.get("subject", ""),
                                    body=p.get("body", ""), in_reply_to=p.get("in_reply_to"),
                                    references=p.get("references", []), received_at=r.created_at, simulated=True))
            last_id = r.id
        return out, {**cursor, "sandbox_last_id": last_id}


class SandboxCalendar:
    name = "sandbox_calendar"
    simulated = True

    def __init__(self, db: Session, calendar_key: str = "default", failure_mode: str | None = None):
        self.db = db
        self.calendar_key = calendar_key
        self.failure_mode = failure_mode

    def _events(self):
        rows = self.db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "CAL_EVENT"))
        return [r for r in rows if r.payload.get("calendar") == self.calendar_key and not r.payload.get("cancelled")]

    def free_busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        if self.failure_mode:
            raise IntegrationError("sandbox: simulated calendar failure", retryable=self.failure_mode == "retryable")
        busy = []
        for r in self._events():
            s, e = datetime.fromisoformat(r.payload["start"]), datetime.fromisoformat(r.payload["end"])
            if s < end and e > start:
                busy.append(BusyInterval(s, e))
        return busy

    def create_event(self, *, summary, start, end, description, attendees, location=None) -> str:
        if self.failure_mode:
            raise IntegrationError("sandbox: simulated calendar failure", retryable=self.failure_mode == "retryable")
        eid = f"sandbox-evt-{uuid.uuid4().hex[:10]}"
        self.db.add(SandboxRecord(kind="CAL_EVENT", payload={
            "calendar": self.calendar_key, "id": eid, "summary": summary, "start": start.isoformat(),
            "end": end.isoformat(), "description": description, "attendees": attendees, "location": location}))
        self.db.flush()
        return eid

    def cancel_event(self, event_id: str) -> None:
        for r in self.db.scalars(select(SandboxRecord).where(SandboxRecord.kind == "CAL_EVENT")):
            if r.payload.get("id") == event_id:
                r.payload = {**r.payload, "cancelled": True}


class SandboxVideo:
    name = "sandbox_video"
    simulated = True

    def __init__(self, db: Session, failure_mode: str | None = None):
        self.db = db
        self.failure_mode = failure_mode

    def create_meeting(self, *, topic, start, duration_minutes) -> VideoMeeting:
        if self.failure_mode:
            raise IntegrationError("sandbox: simulated video provider failure", retryable=self.failure_mode == "retryable")
        mid = str(uuid.uuid4().int)[:11]
        url = f"https://sandbox.invalid/meeting/{mid}"
        self.db.add(SandboxRecord(kind="VIDEO_MEETING", payload={"id": mid, "topic": topic, "start": start.isoformat(),
                                                                 "duration": duration_minutes, "join_url": url}))
        return VideoMeeting(mid, url)


_SIM_NAMES = ["Bayou City", "Brazos Valley", "Gulf Coast", "Lone Star", "Fort Bend", "Riverstone", "Telfair",
              "First Colony", "Greatwood", "Pecan Grove", "Cinco Ranch", "Clear Lake", "Westheimer", "Alief"]
_SIM_SUFFIX = ["Auto Sales", "Motors", "Pre-Owned", "Car Co.", "Auto Group", "Used Cars"]


class SandboxProspectSource:
    """Deterministic simulated dealerships on reserved ``.example`` domains. Never real businesses."""

    name = "SANDBOX"
    simulated = True
    estimated_cost_cents_per_call = 0.0

    def __init__(self, seed: str = "v1", pool_size: int = 60):
        self.seed = seed
        self.pool_size = pool_size

    def search(self, *, city: str, limit: int) -> list[ProspectCandidate]:
        out = []
        for i in range(self.pool_size):
            h = int(hashlib.sha256(f"{self.seed}:{city}:{i}".encode()).hexdigest(), 16)
            name = f"{_SIM_NAMES[h % len(_SIM_NAMES)]} {_SIM_SUFFIX[(h // 7) % len(_SIM_SUFFIX)]} {city[:3].upper()}{i}"
            slug = "".join(ch for ch in name.lower() if ch.isalnum())
            franchise = h % 11 == 0
            out.append(ProspectCandidate(
                name=name + (" Toyota" if franchise else ""),
                source=self.name, source_ref=f"sim-{city.lower().replace(' ', '-')}-{i}",
                address=f"{100 + h % 9000} Simulated Rd", city=city, state="TX", postal_code="77478",
                phone=f"+1555{h % 10_000_000:07d}", website=f"https://{slug}.example",
                email=None if h % 5 == 0 else f"sales@{slug}.example",
                source_url=f"https://{slug}.example", categories=["used_car_dealer"],
                business_status="OPERATIONAL", simulated=True))
            if len(out) >= limit:
                break
        return out


def utc_in(minutes: int) -> datetime:
    return utcnow() + timedelta(minutes=minutes)
