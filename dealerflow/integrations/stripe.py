"""Stripe webhook ingestion (test mode first; live mode only after the owner connects a live endpoint) (snapshot events), verified per Stripe's documented scheme.

Signature: header `Stripe-Signature: t=<ts>,v1=<hex>[,v1=...]`; expected =
HMAC-SHA256(secret, f"{t}.{raw_body}"); constant-time compare; 5-minute
tolerance. Events are de-duplicated by event id (`webhook_events`).

Handled event types (map onto our records; nothing else is inferred):
* invoice.paid               -> confirmed Payment (source=stripe) on the invoice whose
                                external_id matches the Stripe invoice id, or an unapplied
                                payment for the client mapped via metadata.client_id.
* charge.refunded            -> refund CreditNote (source=stripe)
* invoice.payment_failed     -> alert + support ticket
Card data never reaches us: Stripe objects reference payment methods by id only, and
we store none of the payment_method details.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..jobs import enqueue, on_failure
from ..models import Client, Invoice, WebhookEvent
from ..services import alerts, finance
from ..services.tickets import open_system_ticket

TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    pass


def verify_signature(raw_body: bytes, header: str | None, secret: str, now: float | None = None) -> None:
    if not secret:
        raise SignatureError("Stripe webhook secret is not configured")
    if not header:
        raise SignatureError("Missing Stripe-Signature header")
    timestamp, signatures = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            timestamp = v
        elif k == "v1":
            signatures.append(v)
    if timestamp is None or not signatures:
        raise SignatureError("Malformed Stripe-Signature header")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("Invalid timestamp") from exc
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise SignatureError("Signature mismatch")
    if abs((now or time.time()) - ts) > TOLERANCE_SECONDS:
        raise SignatureError("Timestamp outside tolerance")


def sign(raw_body: bytes, secret: str, ts: int | None = None) -> str:
    """Produce a valid header (used by tests and local simulation)."""
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def ingest(db: Session, raw_body: bytes, signature_header: str | None) -> tuple[str, WebhookEvent | None]:
    verify_signature(raw_body, signature_header, get_settings().stripe_webhook_secret.get_secret_value())
    event = json.loads(raw_body)
    event_id = event.get("id")
    if not event_id:
        raise SignatureError("Event has no id")
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.provider == "stripe", WebhookEvent.event_id == event_id))
    if existing is not None:
        return "duplicate", existing
    row = WebhookEvent(provider="stripe", event_id=event_id, event_type=event.get("type"), payload=_minimal(event))
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # concurrent delivery of the same event
        return "duplicate", db.scalar(select(WebhookEvent).where(WebhookEvent.provider == "stripe", WebhookEvent.event_id == event_id))
    enqueue(db, "stripe_event", f"stripe:{event_id}", {"webhook_event_id": row.id}, max_attempts=4)
    db.commit()
    return "accepted", row


def _minimal(event: dict) -> dict:
    """Keep only fields we use; drop anything resembling payment-method details."""
    obj = (event.get("data") or {}).get("object") or {}
    keep = {k: obj.get(k) for k in ("id", "object", "amount_paid", "amount_refunded", "amount", "currency", "customer", "invoice", "status", "metadata", "status_transitions", "created", "number")}
    return {"id": event.get("id"), "type": event.get("type"), "created": event.get("created"), "object": keep}


def process(db: Session, row: WebhookEvent) -> str:
    obj = row.payload.get("object", {})
    etype = row.event_type
    meta = obj.get("metadata") or {}
    client_id = _int(meta.get("client_id"))
    result = "ignored"
    if etype == "invoice.paid":
        inv = db.scalar(select(Invoice).where(Invoice.external_id == obj.get("id")))
        cid = inv.client_id if inv else client_id
        if cid is None or db.get(Client, cid) is None:
            raise ValueError("Cannot map Stripe invoice to a client (set metadata.client_id or link external_id)")
        paid_ts = (obj.get("status_transitions") or {}).get("paid_at") or row.payload.get("created")
        paid_date = datetime.fromtimestamp(int(paid_ts), UTC).date() if paid_ts else utcnow().date()
        amount = int(obj.get("amount_paid") or 0)
        if amount > 0:
            apply_to = inv if inv is not None and inv.status == "issued" and amount <= finance.invoice_balance(inv) else None
            finance.record_payment(
                db, client_id=cid, amount_cents=amount, received_date=paid_date, method="card_via_processor",
                actor=None, invoice=apply_to, source="stripe", external_id=f"stripe:{row.event_id}",
                reference=f"Stripe invoice {obj.get('id')}", currency=(obj.get("currency") or "usd").upper(),
            )
            result = "payment_recorded"
    elif etype == "charge.refunded":
        inv = db.scalar(select(Invoice).where(Invoice.external_id == obj.get("invoice"))) if obj.get("invoice") else None
        cid = inv.client_id if inv else client_id
        if cid is None:
            raise ValueError("Cannot map refund to a client")
        amount = int(obj.get("amount_refunded") or 0)
        if amount > 0:
            finance.record_credit_note(
                db, client_id=cid, kind="refund", amount_cents=amount, issued_date=utcnow().date(),
                reason=f"Stripe refund on charge {obj.get('id')}", actor=None, invoice=None,
                source="stripe", external_id=f"stripe:{row.event_id}",
            )
            result = "refund_recorded"
    elif etype == "invoice.payment_failed":
        inv = db.scalar(select(Invoice).where(Invoice.external_id == obj.get("id")))
        cid = inv.client_id if inv else client_id
        alerts.raise_alert(db, "payment_failed", f"Stripe reports a failed payment for invoice {obj.get('id')}", "warning", f"stripe_fail:{row.event_id}")
        if cid:
            open_system_ticket(db, client_id=cid, kind="client_issue", title="Payment failed (Stripe)", description=f"Stripe invoice {obj.get('id')}", dedupe_key=f"stripe_fail:{obj.get('id')}")
        result = "failure_recorded"
    row.status = "processed"
    row.processed_at = utcnow()
    return result


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@on_failure("stripe_event")
def _stripe_failed(db: Session, payload: dict, error: str) -> None:
    row = db.get(WebhookEvent, payload.get("webhook_event_id"))
    if row is not None:
        row.status, row.error = "failed", error[:2000]
    alerts.raise_alert(db, "webhook_failed", f"Stripe event could not be processed: {error[:300]}", "error",
                       f"stripe_failed:{payload.get('webhook_event_id')}")
