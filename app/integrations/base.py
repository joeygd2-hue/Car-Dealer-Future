"""Provider-neutral integration contracts shared by all phases.

Adapters never raise for expected provider failures; they return a result object with
``retryable`` so job handlers can decide whether to retry (RetryLater/exception) or stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# Delivery statuses that callers persist verbatim.
SENT = "SENT"
SIMULATED = "SIMULATED"  # sandbox: nothing left the system
FAILED = "FAILED"


@dataclass
class SendResult:
    status: str
    provider_message_id: str | None = None
    error: str | None = None
    retryable: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (SENT, SIMULATED)

    @property
    def simulated(self) -> bool:
        return self.status == SIMULATED


class IntegrationError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class CustomerChannel(Protocol):
    provider: str
    supports_send: bool

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult: ...


class EmailSender(Protocol):
    name: str

    def send(self, *, to: str, subject: str, body: str, from_addr: str, from_name: str | None = None,
             headers: dict[str, str] | None = None, message_id: str | None = None,
             in_reply_to: str | None = None) -> SendResult: ...


@dataclass
class InboundEmail:
    message_id: str
    from_addr: str
    subject: str
    body: str
    in_reply_to: str | None
    references: list[str]
    received_at: datetime
    simulated: bool = False


class InboxReader(Protocol):
    name: str

    def fetch_new(self, cursor: dict) -> tuple[list[InboundEmail], dict]: ...


@dataclass
class BusyInterval:
    start: datetime
    end: datetime


class CalendarProvider(Protocol):
    name: str
    simulated: bool

    def free_busy(self, start: datetime, end: datetime) -> list[BusyInterval]: ...

    def create_event(self, *, summary: str, start: datetime, end: datetime, description: str,
                     attendees: list[str], location: str | None = None) -> str: ...

    def cancel_event(self, event_id: str) -> None: ...


@dataclass
class VideoMeeting:
    meeting_id: str
    join_url: str


class VideoMeetingProvider(Protocol):
    name: str
    simulated: bool

    def create_meeting(self, *, topic: str, start: datetime, duration_minutes: int) -> VideoMeeting: ...


@dataclass
class ProspectCandidate:
    name: str
    source: str
    source_ref: str | None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    source_url: str | None = None
    categories: list[str] = field(default_factory=list)
    business_status: str | None = None
    simulated: bool = False
    content_ttl_days: int | None = None  # provider terms limit storage of some fields


class ProspectSource(Protocol):
    name: str
    simulated: bool
    estimated_cost_cents_per_call: float

    def search(self, *, city: str, limit: int) -> list[ProspectCandidate]: ...


class LLMProvider(Protocol):
    name: str

    def complete(self, *, system: str, prompt: str, max_tokens: int = 800) -> tuple[str | None, float]:
        """Returns (text or None on refusal/failure, estimated cost in cents)."""
        ...
