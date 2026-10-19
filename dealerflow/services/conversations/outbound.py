"""Outbound customer messages (ported from Phase 3 ``services/inbox/outbound.py``).

Send pipeline (``conversation_send`` job), designed so that a crash can never cause a silent duplicate:
  1. atomic claim ``queued`` → ``sending`` (only one worker can win), stamping ``sending_started_at``;
  2. re-check every stop condition (emergency stop, opt-out, suppression, pauses, takeover, channel);
  3. automatic/system messages: re-validate the FINAL text against the CURRENT verified facts;
  4. automatic messages: reserve database rate-limit slots (shared by every process);
  5. COMMIT, then deliver through ``track_integration`` (which re-checks the emergency stop), then record the result.
A job that finds a message still ``sending`` after the job lock timeout (its worker died after step 5 began) marks it
``uncertain``: staff must check and either mark it delivered or re-queue it. It is never re-sent automatically.

Delivery: only website chat can be delivered by the platform (the reply is stored and the dealership's widget
shows it). It is live only when ``DF_CUSTOMER_MESSAGING_LIVE_ENABLED`` is true AND the dealership has a
``customer_messaging``/``website_chat`` connection marked ``connected``; otherwise the reply is recorded as
``simulated`` and never shown to the visitor. ADF and Marketplace replies are sent by staff outside the platform.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...audit import track_integration
from ...config import get_settings
from ...db import utcnow
from ...integrations.errors import ExternalActionsHalted
from ...jobs import STALE_LOCK, DeferJob, enqueue, handler, on_failure
from ...models import (
    Appointment,
    Client,
    Conversation,
    ConversationMessage,
    CustomerContact,
    CustomerSuppression,
    ResponseDraft,
)
from ...models_conversations import SENDABLE_CHANNELS, WEBSITE_CHAT
from ...models_dealer import Vehicle
from .. import rate_limits
from . import notify
from .common import conversation_link, log, utc_to_local
from .policy import automation_blockers, channel_connection, emergency_stopped, global_pause, is_suppressed, settings_row

JOB = "conversation_send"


def queue_outbound(db: Session, conv: Conversation, body: str, *, sender_type: str, user_id: int | None = None,
                   draft: ResponseDraft | None = None, idempotency_key: str | None = None) -> ConversationMessage:
    if idempotency_key:
        existing = db.scalar(select(ConversationMessage).where(ConversationMessage.idempotency_key == idempotency_key))
        if existing:
            return existing
    msg = ConversationMessage(client_id=conv.client_id, conversation_id=conv.id, channel=conv.channel, direction="outbound",
                              sender_type=sender_type, sender_user_id=user_id, body=body, language=conv.preferred_language,
                              delivery_status="queued", idempotency_key=idempotency_key)
    db.add(msg)
    db.flush()
    if draft is not None:
        draft.sent_message_id = msg.id
    enqueue_send(db, msg)
    log(db, "conversation.message_queued", client_id=conv.client_id, entity_type="conversation_message", entity_id=msg.id,
        actor_id=user_id, actor_type="user" if user_id else "system", sender_type=sender_type, conversation_id=conv.id)
    return msg


def enqueue_send(db: Session, msg: ConversationMessage, generation: int = 0) -> None:
    key = f"{JOB}:{msg.id}" if generation == 0 else f"{JOB}:{msg.id}:r{generation}"
    enqueue(db, JOB, key, {"message_id": msg.id, "client_id": msg.client_id}, max_attempts=5)


def record_manual_external(db: Session, conv: Conversation, body: str, *, user_id: int,
                           draft: ResponseDraft | None = None) -> ConversationMessage:
    """Staff sent this reply themselves outside the platform (e.g. pasted into Facebook Marketplace)."""
    msg = ConversationMessage(client_id=conv.client_id, conversation_id=conv.id, channel=conv.channel, direction="outbound",
                              sender_type="staff", sender_user_id=user_id, body=body, language=conv.preferred_language,
                              delivery_status="manual_external", sent_at=utcnow())
    db.add(msg)
    db.flush()
    if draft is not None:
        draft.sent_message_id, draft.status = msg.id, "sent"
        draft.reviewed_by_id, draft.reviewed_at = user_id, utcnow()
    _mark_responded(conv)
    log(db, "conversation.manual_external_recorded", client_id=conv.client_id, entity_type="conversation_message",
        entity_id=msg.id, actor_id=user_id, actor_type="user", channel=conv.channel)
    return msg


def _mark_responded(conv: Conversation, *, delivered: bool = True) -> None:
    """Response-time metrics count only replies that reached the customer (sent, or sent by staff themselves)."""
    if delivered:
        now = utcnow()
        conv.last_outbound_at = now
        conv.first_response_at = conv.first_response_at or now
    if conv.status != "escalated":
        conv.status = "awaiting_customer"


def live_delivery(db: Session, conv: Conversation) -> tuple[bool, str]:
    """Whether a reply on this conversation would really reach the customer."""
    if conv.channel != WEBSITE_CHAT:
        return False, "manual channel"
    if not get_settings().customer_messaging_live_enabled:
        return False, "DF_CUSTOMER_MESSAGING_LIVE_ENABLED is false (simulated)"
    conn = channel_connection(db, conv.client_id, WEBSITE_CHAT)
    if conn is None or conn.status != "connected":
        return False, "website chat is not an authorized, connected channel for this dealership (simulated)"
    return True, "website chat (live authorized)"


def send_blockers(db: Session, msg: ConversationMessage, conv: Conversation) -> list[str]:
    reasons = []
    if emergency_stopped(db):
        reasons.append("platform emergency stop is active")
    if conv.opted_out:
        reasons.append("customer opted out")
    if is_suppressed(db, conv):
        reasons.append("recipient on the dealership opt-out list")
    if conv.channel not in SENDABLE_CHANNELS:
        reasons.append(f"{conv.channel} replies must be sent manually by staff")
    if msg.sender_type == "ai_auto":
        reasons += automation_blockers(db, conv, settings_row(db, conv.client_id))
    elif msg.sender_type == "system":
        if global_pause(db):
            reasons.append("platform-wide conversation automation pause is on")
        if settings_row(db, conv.client_id).paused:
            reasons.append("dealership automation is paused")
    return list(dict.fromkeys(reasons))


def current_fact_snapshot(db: Session, conv: Conversation):
    from .responder.facts import build_fact_sheet

    client = db.get(Client, conv.client_id)
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    fs = build_fact_sheet(db, client, settings_row(db, client.id), conv.preferred_language, vehicle)
    return {f.key: f.value for f in fs.facts}, fs


def final_content_problems(db: Session, msg: ConversationMessage, conv: Conversation) -> list[str]:
    """Validate the exact text about to be sent (not just the earlier draft)."""
    from .responder.engine import T, describe_when
    from .responder.safety import validate_output

    if msg.sender_type == "ai_auto":
        draft = db.scalar(select(ResponseDraft).where(ResponseDraft.client_id == conv.client_id,
                                                      ResponseDraft.sent_message_id == msg.id))
        snapshot_now, fs = current_fact_snapshot(db, conv)
        problems = [f"verified fact '{k}' changed or became unverified since the draft"
                    for k, v in ((draft.fact_snapshot or {}) if draft else {}).items() if snapshot_now.get(k) != v]
        allowed = fs.allowed_strings() + [x[conv.preferred_language] for x in T.values()]
        return problems + validate_output(msg.body, allowed_sources=allowed, language=conv.preferred_language)
    if msg.sender_type == "system" and (msg.idempotency_key or "").startswith("appt:"):
        from .appointments import CUSTOMER_TEXT

        _, appt_id, kind, start_iso = msg.idempotency_key.split(":", 3)
        appt = db.get(Appointment, int(appt_id))
        if appt is None or appt.client_id != conv.client_id:
            return ["appointment not found"]
        expected = "cancelled" if kind == "cancellation" else "confirmed"
        start = appt.confirmed_start or appt.requested_start
        if appt.status != expected or start is None or start.isoformat() != start_iso:
            return [f"appointment changed (now {appt.status}); {kind} message no longer valid"]
        client = db.get(Client, conv.client_id)
        when = describe_when(utc_to_local(start, client.timezone), conv.preferred_language)
        allowed = [client.name, when, CUSTOMER_TEXT[kind][conv.preferred_language]]
        return validate_output(msg.body, allowed_sources=allowed, language=conv.preferred_language,
                               appointment_confirmed=kind != "cancellation")
    return []


def _block(db: Session, msg: ConversationMessage, reasons: list[str], *, back_to_review: bool = False) -> dict:
    msg.delivery_status, msg.delivery_error = "blocked", "; ".join(reasons)
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.client_id == msg.client_id, ResponseDraft.sent_message_id == msg.id))
    if draft:
        if back_to_review:
            draft.status, draft.sent_message_id, draft.decision = "pending_review", None, "draft_only"
            draft.decision_reasons = list(draft.decision_reasons or []) + [f"blocked at send time: {r}" for r in reasons]
        else:
            draft.status = "blocked"
    log(db, "conversation.message_blocked", client_id=msg.client_id, entity_type="conversation_message", entity_id=msg.id,
        actor_type="worker", reasons=reasons)
    return {"blocked": reasons}


def _alert(db: Session, msg: ConversationMessage, *, uncertain: bool) -> None:
    conv = db.get(Conversation, msg.conversation_id)
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.client_id == msg.client_id, ResponseDraft.sent_message_id == msg.id))
    if draft and not uncertain:
        draft.status = "send_failed"
    log(db, "conversation.delivery_uncertain" if uncertain else "conversation.send_failed", client_id=msg.client_id,
        entity_type="conversation_message", entity_id=msg.id, actor_type="worker", error=(msg.delivery_error or "")[:300])
    subject = (f"Delivery uncertain (conversation #{conv.id}) — check before resending" if uncertain
               else f"Reply failed to send (conversation #{conv.id})")
    body = ("We can't confirm whether this reply reached the customer. It will NOT be re-sent automatically. "
            "Check the channel, then mark it delivered or re-queue it." if uncertain
            else f"A reply could not be delivered: {msg.delivery_error}. Please follow up manually.")
    notify.notify(db, msg.client_id, kind="system", subject=subject, body=body, link=conversation_link(conv.client_id, conv.id),
                  assigned_user_id=conv.assigned_user_id, dedupe_key=f"sendfail:{msg.id}:{'u' if uncertain else 'f'}")


@on_failure(JOB)
def _on_dead(db: Session, payload: dict, error: str) -> None:
    msg = db.get(ConversationMessage, payload.get("message_id"))
    if msg and msg.delivery_status in ("queued", "sending"):
        msg.delivery_status, msg.delivery_error = ("uncertain" if msg.delivery_status == "sending" else "failed"), error[:1000]
        _alert(db, msg, uncertain=msg.delivery_status == "uncertain")


@handler(JOB)
def send_job(db: Session, payload: dict) -> dict:
    msg = db.get(ConversationMessage, payload["message_id"])
    if msg is None or msg.client_id != payload.get("client_id", msg.client_id):
        return {"skipped": "missing"}
    if msg.delivery_status == "sending":
        started = msg.sending_started_at or utcnow()
        if utcnow() - started < STALE_LOCK:  # its worker may still be finishing: never overwrite its outcome
            raise DeferJob(started + STALE_LOCK + timedelta(seconds=5), "another worker is sending this message")
        moved = db.execute(update(ConversationMessage).where(ConversationMessage.id == msg.id,
                                                             ConversationMessage.delivery_status == "sending")
                           .values(delivery_status="uncertain",
                                   delivery_error="worker stopped after the send began; delivery unknown — not re-sent")
                           .execution_options(synchronize_session=False)).rowcount
        if moved != 1:
            return {"skipped": "owner finished"}
        db.refresh(msg)
        _alert(db, msg, uncertain=True)
        return {"uncertain": True}
    claimed = db.execute(update(ConversationMessage).where(ConversationMessage.id == msg.id,
                                                           ConversationMessage.delivery_status == "queued")
                         .values(delivery_status="sending", sending_started_at=utcnow())
                         .execution_options(synchronize_session=False)).rowcount
    if claimed != 1:
        return {"skipped": msg.delivery_status}
    db.refresh(msg)
    conv = db.get(Conversation, msg.conversation_id)
    blockers = send_blockers(db, msg, conv)
    if blockers:
        return _block(db, msg, blockers, back_to_review=msg.sender_type in ("ai_auto", "staff"))
    problems = final_content_problems(db, msg, conv)
    if problems:
        out = _block(db, msg, ["final message failed validation: " + "; ".join(problems)],
                     back_to_review=msg.sender_type == "ai_auto")
        if msg.sender_type == "ai_auto":
            from . import escalation

            escalation.open_or_update(db, conv, ["uncertain_inventory"], None)
        return out
    if msg.sender_type == "ai_auto":
        s = settings_row(db, conv.client_id)
        failed = rate_limits.acquire_all(db, [
            (f"auto:conv:{conv.id}", s.max_auto_replies_per_conversation_per_hour, 3600, 1),
            (f"auto:client:{conv.client_id}", s.max_auto_replies_per_dealership_per_hour, 3600, 1),
        ])
        if failed:
            return _block(db, msg, [f"auto-reply rate limit reached ({failed})"], back_to_review=True)
    live, note = live_delivery(db, conv)
    db.commit()  # the claim is durable before anything can reach the customer
    try:
        with track_integration(db, "website_chat", "send_reply", simulated=not live) as ctx:
            ctx["detail"] = note
            status = "sent" if live else "simulated"  # first-party chat: "delivery" is storing the reply for the widget
    except ExternalActionsHalted as exc:  # the gate raises before anything is delivered
        return _block(db, msg, [str(exc)], back_to_review=msg.sender_type in ("ai_auto", "staff"))
    msg.delivery_status, msg.sent_at = status, utcnow()
    msg.delivery_error = None if live else note
    _mark_responded(conv, delivered=live)
    draft = db.scalar(select(ResponseDraft).where(ResponseDraft.client_id == msg.client_id, ResponseDraft.sent_message_id == msg.id))
    if draft and draft.status == "pending_review":
        draft.status = "sent"
    log(db, "conversation.message_sent" if live else "conversation.message_simulated", client_id=conv.client_id,
        entity_type="conversation_message", entity_id=msg.id, actor_type="worker", channel=conv.channel,
        sender_type=msg.sender_type, live=live)
    return {"status": status}


def reconcile_uncertain(db: Session, msg: ConversationMessage, *, delivered: bool, user_id: int) -> None:
    """Staff decision for an ``uncertain`` message after checking the channel."""
    if msg.delivery_status != "uncertain":
        raise ValueError("message is not awaiting reconciliation")
    if delivered:
        msg.delivery_status, msg.delivery_error = "sent", "marked delivered by staff after reconciliation"
    else:
        msg.delivery_status, msg.delivery_error, msg.sending_started_at = "queued", None, None
        generation = int((msg.analysis or {}).get("requeues", 0)) + 1  # a new job key per re-queue
        msg.analysis = {**(msg.analysis or {}), "requeues": generation}
        enqueue_send(db, msg, generation)
    log(db, "conversation.message_reconciled", client_id=msg.client_id, entity_type="conversation_message", entity_id=msg.id,
        actor_id=user_id, actor_type="user", delivered=delivered)


def mark_opted_out(db: Session, conv: Conversation, *, source: str) -> None:
    if conv.opted_out:
        return
    now = utcnow()
    conv.opted_out, conv.opted_out_at = True, now
    if conv.contact_id:
        contact = db.get(CustomerContact, conv.contact_id)
        contact.opted_out_at = now
    if not is_suppressed(db, conv):
        db.add(CustomerSuppression(client_id=conv.client_id, channel=conv.channel, address=conv.external_conversation_id,
                                   reason="opt_out", source=source[:200]))
    for d in db.scalars(select(ResponseDraft).where(ResponseDraft.client_id == conv.client_id,
                                                    ResponseDraft.conversation_id == conv.id,
                                                    ResponseDraft.status == "pending_review")):
        d.status = "superseded"
    for m in db.scalars(select(ConversationMessage).where(
            ConversationMessage.client_id == conv.client_id, ConversationMessage.conversation_id == conv.id,
            ConversationMessage.delivery_status == "queued")):
        m.delivery_status, m.delivery_error = "blocked", "customer opted out"
    log(db, "conversation.opted_out", client_id=conv.client_id, entity_type="conversation", entity_id=conv.id,
        actor_type="customer", source=source[:200])
