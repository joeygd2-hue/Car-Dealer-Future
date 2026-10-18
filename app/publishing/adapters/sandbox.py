"""Sandbox channel: a simulated asynchronous API used for tests and demos.
It never contacts any external platform. Failures can be injected through the
connection config: {"fail_next": 2, "fail_mode": "retryable" | "permanent"}."""
from __future__ import annotations

from app.core.time import new_id
from app.publishing.adapters.base import AdapterError, AdapterResult, ChannelAdapter


class SandboxAdapter(ChannelAdapter):
    key = "sandbox"
    label = "Sandbox channel (simulated, test only)"
    mode = "sandbox"
    requires_connection = False
    DOCS = {
        "integration": "Simulated. Never contacts a real platform.",
        "supported_operations": ["create", "update", "delete", "status check"],
        "credentials": "None", "fields": ["title", "description", "price", "photos"], "media": "Photo URLs",
        "rate_limits": "None", "confirmation": "Asynchronous: submitted, then confirmed on status check.",
        "update_delete": "Supported", "failure_recovery": "Injectable retryable/permanent failures for testing.",
        "status": "Test fixture",
    }
    _state: dict[str, int] = {}

    def build_payload(self, *, content, vehicle, facts, connection_config):
        return {"id": vehicle.id, "title": content["en"]["title"], "description": content["en"]["description"],
                "price": facts.get("price_usd"), "photos": facts["photos"]}

    def _maybe_fail(self, connection):
        cfg = (connection.config if connection else {}) or {}
        key = connection.id if connection else "default"
        used = self._state.get(key, 0)
        if used < int(cfg.get("fail_next", 0)):
            self._state[key] = used + 1
            raise AdapterError("Simulated provider failure", retryable=cfg.get("fail_mode", "retryable") == "retryable")

    def publish(self, payload, *, connection, idempotency_key):
        self._maybe_fail(connection)
        return AdapterResult(outcome="submitted", external_id=payload["id"], provider_ref=new_id("sbx"),
                             external_url=f"https://sandbox.invalid/listings/{payload['id']}")

    def delete(self, *, connection, external_id, idempotency_key, payload):
        self._maybe_fail(connection)
        return AdapterResult(outcome="confirmed")

    def check_status(self, *, connection, provider_ref, payload):
        return AdapterResult(outcome="confirmed")
