"""Channel adapter contract. Each adapter documents its real capabilities in
``DOCS`` (surfaced in the API and docs/architecture/integration-matrix.md).

mode:
  api     - a real, authorised API integration (may still require credentials/approval)
  manual  - we prepare everything; a human publishes and records the result
  sandbox - simulated API for tests and demos; never reaches a real platform
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterResult:
    outcome: str  # confirmed | submitted | manual_required | failed
    external_id: str | None = None
    external_url: str | None = None
    provider_ref: str | None = None
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    retryable: bool = False


class AdapterError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ChannelAdapter:
    key = "base"
    label = "Base"
    mode = "manual"
    requires_connection = False
    DOCS: dict[str, Any] = {}

    def build_payload(self, *, content: dict, vehicle, facts: dict, connection_config: dict) -> dict[str, Any]:
        raise NotImplementedError

    def validate_payload(self, payload: dict[str, Any]) -> list[str]:
        return []

    def publish(self, payload: dict, *, connection, idempotency_key: str) -> AdapterResult:
        raise NotImplementedError

    def update(self, payload: dict, *, connection, idempotency_key: str, external_id: str | None) -> AdapterResult:
        return self.publish(payload, connection=connection, idempotency_key=idempotency_key)

    def delete(self, *, connection, external_id: str | None, idempotency_key: str, payload: dict) -> AdapterResult:
        raise NotImplementedError

    def check_status(self, *, connection, provider_ref: str, payload: dict) -> AdapterResult:
        return AdapterResult(outcome="confirmed")
