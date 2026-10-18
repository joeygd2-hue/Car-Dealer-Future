"""Provider interfaces for the outreach agent. Each implementation states
whether it is live (reaches a real service) or sandbox."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class OutboundEmail:
    to: str
    from_address: str
    from_name: str
    subject: str
    body: str
    idempotency_key: str
    thread_id: str | None = None
    in_reply_to: str | None = None
    unsubscribe_url: str | None = None


@dataclass
class SendResult:
    provider_message_id: str
    thread_id: str | None


@dataclass
class InboundEmail:
    provider_message_id: str
    thread_id: str | None
    from_address: str
    to_address: str
    subject: str
    body: str
    received_at: datetime
    in_reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Candidate:
    business_name: str
    source: str
    source_ref: str | None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    types: list[str] = field(default_factory=list)
    business_status: str | None = None
    simulated: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Slot:
    start: datetime
    end: datetime


class EmailProvider:
    live = False
    name = "base"

    def send(self, email: OutboundEmail) -> SendResult:
        raise NotImplementedError

    def fetch_inbound(self, since: datetime | None, limit: int = 50) -> list[InboundEmail]:
        raise NotImplementedError

    def health_check(self) -> tuple[str, str]:
        return "ok", "not checked"


class CalendarProvider:
    live = False
    name = "base"

    def busy(self, start: datetime, end: datetime) -> list[Slot]:
        raise NotImplementedError

    def create_event(self, *, start: datetime, end: datetime, attendee_email: str, summary: str, description: str,
                     idempotency_key: str) -> tuple[str, str | None]:
        raise NotImplementedError

    def health_check(self) -> tuple[str, str]:
        return "ok", "not checked"


class DiscoveryProvider:
    live = False
    name = "base"
    estimated_cost_per_call = 0.0

    def search(self, query: str, limit: int = 20) -> list[Candidate]:
        raise NotImplementedError

    def health_check(self) -> tuple[str, str]:
        return "ok", "not checked"
