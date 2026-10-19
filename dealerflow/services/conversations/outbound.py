"""Outbound customer messages.

Send pipeline (``conversations.outbound_send`` job), designed so that a crash can never cause a silent duplicate:
  1. atomic claim QUEUED → SENDING (only one worker can win)
  2. re-check every stop condition (opt-out, suppression, pauses, takeover, consent, messaging window)
  3. AI/system messages: re-validate the FINAL text against the CURRENT verified facts
  4. AI messages: reserve distributed rate-limit slots
  5. send gate: platform emergency stop; SendAttempt row IN_FLIGHT
  6. COMMIT, then call the provider, then record the result
If a job finds a message already SENDING (previous worker died after step 6 began), the message becomes
UNCERTAIN and an operator must reconcile it; it is never re-sent automatically.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...db import utcnow
from ...integrations.errors import IntegrationError
from ...jobs import DeferJob, enqueue, handler, on_failure
from ...models import (
    Appointment,
    Client,
    ContactSuppression,
    Conversation,
    CustomerContact,
    ResponseDraft,
    SendAttempt,
    Vehicle,
)
from ...models import ConversationMessage as Message
from . import notifications, rate_limits, send_gate
from .automation_policy import GLOBAL_AUTOMATION_PAUSE, automation_blockers, get_settings_row
from .channels import META_MESSENGER, SENDABLE_CHANNELS, TWILIO_SMS, customer_channel
from .common import get_flag, record
from .summaries import conversation_link

KIND = "CUSTOMER_MESSAGE"


def queue_outbound(db: Session, conv: Conversation, body: str, *, sender_type: str, user_id: int | None = None,
                   draft: ResponseDraft | None = None, idempotency_key: str | None = None) -> Message:
    if idempotency_key:
        existing = db.scalar(select(Message).where(Message.idempotency_key == idempotency_key))
        if existing is not None:  # the same logical message was already queued (job retry, duplicate event)
            return existing
    msg = Message(conversation_id=conv.id, client_id=conv.client_id, channel=conv.channel, direction="OUTBOUND",
                  sender_type=sender_type, sender_user_id=user_id, body=body, language=conv.preferred_language,
                  delivery_status="QUEUED", idempotency_key=idempotency_key)
    db.add(msg)
    db.flush()
    if draft is not None:
        draft.sent_message_id = msg.id
    enqueue_send(db, msg)
    record(db, "message.queued", client_id=conv.client_id, actor_id=user_id, entity_type="message", entity_id=msg.id,
           sender_type=sender_type)
    return msg


def enqueue_send(db: Session, msg: Message, generation: int = 0) -> None:
    key = f"conv-outbound:{msg.id}" if generation == 0 else f"conv-outbound:{msg.id}:r{generation}"
    enqueue(db, "conversations.outbound_send", key, {"message_id": msg.id}, max_attempts=5)


def record_manual_external(db: Session, conv: Conversation, body: str, *, user_id: int,
                           draft: ResponseDraft | None = None) -> Message:
    """Staff sent this reply themselves outside the platform (e.g. pasted into Marketplace)."""
    msg = Message(conversation_id=conv.id, client_id=conv.client_id, channel=conv.channel, direction="OUTBOUND",
                  sender_type="STAFF", sender_user_id=user_id, body=body, language=conv.preferred_language,
                  delivery_status="MANUAL_EXTERNAL")
    db.add(msg)
    db.flush()
    if draft is not None:
        draft.sent_message_id = msg.id
        draft.status = "SENT"
        draft.reviewed_by_id = user_id
        draft.reviewed_at = utcnow()
    conv.last_outbound_at = utcnow()
    conv.status = "AWAITING_CUSTOMER"
    record(db, "message.manual_external_recorded", client_id=conv.client_id, actor_id=user_id, entity_type="message",
           entity_id=msg.id, channel=conv.channel)
    return msg


def _send_blockers(db: Session, msg: Message, conv: Conversation) -> list[str]:
    reasons = []
    if conv.opted_out:
        reasons.append("customer opted out")
    contact = db.get(CustomerContact, conv.contact_id) if conv.contact_id else None
    if db.scalar(select(ContactSuppression.id).where(ContactSuppression.client_id == conv.client_id,
                                                     ContactSuppression.channel == conv.channel,
                                                     ContactSuppression.address == conv.external_conversation_id)):
        reasons.append("recipient on opt-out list")
    if conv.channel not in SENDABLE_CHANNELS:
        reasons.append(f"{conv.channel} replies must be sent manually by staff")
    if msg.sender_type == "AI_AUTO":
        reasons += automation_blockers(db, conv, get_settings_row(db, conv.client_id))
    elif msg.sender_type == "SYSTEM":
        if get_flag(db, GLOBAL_AUTOMATION_PAUSE):
            reasons.append("global automation pause is on")
        if get_settings_row(db, conv.client_id).paused:
            reasons.append("dealership automation is paused")
        if conv.channel == TWILIO_SMS and not (contact and contact.sms_consent):
            reasons.append("no recorded SMS consent for automated message")
    if conv.channel == META_MESSENGER:
        last_in = conv.last_inbound_at
        age = (utcnow() - last_in) if last_in else timedelta(days=999)
        if age > timedelta(days=7):
            reasons.append("outside Messenger 7-day human-agent window")
        elif age > timedelta(hours=24) and msg.sender_type != "STAFF":
            reasons.append("outside Messenger 24-hour window (only staff may reply with HUMAN_AGENT tag)")
    return list(dict.fromkeys(reasons))


def current_fact_snapshot(db: Session, conv: Conversation) -> dict:
    """The verified facts as of right now (same rules the engine uses)."""
    from .responder.facts import build_fact_sheet

    client = db.get(Client, conv.client_id)
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    fs = build_fact_sheet(db, client, get_settings_row(db, client.id), conv.preferred_language, vehicle)
    return {f.key: f.value for f in fs.facts}, fs


def final_content_problems(db: Session, msg: Message, conv: Conversation) -> list[str]:
    """Validate the exact text about to be sent (not just the earlier draft)."""
    from .responder.engine import T, describe_when
    from .responder.safety import validate_output

    if msg.sender_type == "AI_AUTO":
        draft = _draft_for(db, msg)
        snapshot_now, fs = current_fact_snapshot(db, conv)
        problems = []
        for key, value in ((draft.fact_snapshot or {}) if draft else {}).items():
            if snapshot_now.get(key) != value:
                problems.append(f"verified fact '{key}' changed or became unverified since the draft")
        allowed = fs.allowed_strings() + [x[conv.preferred_language] for x in T.values()]
        problems += validate_output(msg.body, allowed_sources=allowed, language=conv.preferred_language)
        return problems
    if msg.sender_type == "SYSTEM" and (msg.idempotency_key or "").startswith("appt:"):
        from .appointments import CUSTOMER_TEXT
        from .common import utc_to_local

        _, appt_id, tag, start_iso = msg.idempotency_key.split(":", 3)
        kind = tag.split("-", 1)[0]  # "reminder-24h" -> "reminder"
        appt = db.get(Appointment, int(appt_id))
        if appt is None or appt.client_id != conv.client_id:
            return ["appointment not found"]
        expected_status = "CANCELLED" if kind == "cancellation" else "CONFIRMED"
        start = appt.confirmed_start or appt.requested_start
        if appt.status != expected_status or start.isoformat() != start_iso:
            return [f"appointment changed (now {appt.status}); {kind} message no longer valid"]
        client = db.get(Client, conv.client_id)
        when = describe_when(utc_to_local(start, client.timezone), conv.preferred_language)
        allowed = [client.name, when, CUSTOMER_TEXT[kind][conv.preferred_language]]
        return validate_output(msg.body, allowed_sources=allowed, language=conv.preferred_language,
                               appointment_confirmed=kind != "cancellation")
    return []


def _draft_for(db: Session, msg: Message) -> ResponseDraft | None:
    return db.scalar(select(ResponseDraft).where(ResponseDraft.client_id == msg.client_id,
                                                 ResponseDraft.sent_message_id == msg.id))


def _block(db: Session, msg: Message, reasons: list[str], *, back_to_review: bool = False) -> dict:
    msg.delivery_status = "BLOCKED"
    msg.delivery_error = "; ".join(reasons)
    draft = _draft_for(db, msg)
    if draft:
        draft.status = "PENDING_REVIEW" if back_to_review else "BLOCKED"
        if back_to_review:
            draft.sent_message_id = None
            draft.decision = "DRAFT_ONLY"
            draft.decision_reasons = list(draft.decision_reasons or []) + [f"auto-send blocked at send time: {r}" for r in reasons]
    record(db, "message.blocked", client_id=msg.client_id, entity_type="message", entity_id=msg.id, outcome="BLOCKED",
           reasons=reasons)
    return {"blocked": reasons}


@on_failure("conversations.outbound_send")
def _on_dead(db: Session, payload: dict, error: str) -> None:
    msg = db.get(Message, payload.get("message_id"))
    if msg and msg.delivery_status in ("QUEUED", "SENDING"):
        msg.delivery_status = "FAILED"
        msg.delivery_error = (error or "")[:1000]
        _failure_alert(db, msg)


def _failure_alert(db: Session, msg: Message, *, uncertain: bool = False) -> None:
    conv = db.get(Conversation, msg.conversation_id)
    draft = _draft_for(db, msg)
    if draft and not uncertain:
        draft.status = "SEND_FAILED"
    record(db, "message.delivery_uncertain" if uncertain else "message.send_failed", client_id=msg.client_id,
           entity_type="message", entity_id=msg.id, outcome="FAILED", error=msg.delivery_error)
    subject = (f"Delivery uncertain (conversation #{conv.id}) — check before resending" if uncertain
               else f"Message failed to send (conversation #{conv.id})")
    body = (f"We can't confirm whether a reply on {conv.channel} reached the customer: {msg.delivery_error}\n\n"
            "It will NOT be re-sent automatically. Check the channel, then mark it delivered or re-queue it."
            if uncertain else
            f"A reply on {conv.channel} could not be delivered: {msg.delivery_error}\n\nPlease follow up manually.")
    notifications.notify(db, msg.client_id, kind="SYSTEM", subject=subject, body=body, link=conversation_link(conv),
                         assigned_user_id=conv.assigned_user_id, dedupe_key=f"sendfail:{msg.id}:{'u' if uncertain else 'f'}")
    notifications.notify_operator(db, subject=subject, body=body, dedupe_key=f"op-sendfail:{msg.id}:{'u' if uncertain else 'f'}")


@handler("conversations.outbound_send")
def send_job(db: Session, payload: dict) -> dict:
    msg_id = int(payload["message_id"])
    msg = db.get(Message, msg_id)
    if msg is None:
        return {"skipped": "missing"}
    if msg.delivery_status == "SENDING":
        verdict, wait, attempt = send_gate.check_sending_row(db, KIND, msg.id)
        if verdict != "crashed":  # owner may be alive or just finished: never overwrite its outcome
            raise DeferJob(utcnow() + send_gate.defer_seconds(wait), "another worker is sending (or just sent) this message")
        if not send_gate.take_over_crashed(db, attempt, Message, "delivery_status", msg.id):
            return {"skipped": "owner finished"}
        db.refresh(msg)
        msg.delivery_error = "worker stopped mid-send; delivery unknown — not re-sent automatically"
        _failure_alert(db, msg, uncertain=True)
        return {"uncertain": True}
    claimed = db.execute(update(Message).where(Message.id == msg_id, Message.delivery_status == "QUEUED")
                         .values(delivery_status="SENDING").execution_options(synchronize_session=False)).rowcount
    if claimed != 1:
        return {"skipped": msg.delivery_status}
    db.refresh(msg)
    conv = db.get(Conversation, msg.conversation_id)
    blockers = _send_blockers(db, msg, conv)
    if blockers:
        return _block(db, msg, blockers)
    problems = final_content_problems(db, msg, conv)
    if problems:
        out = _block(db, msg, ["final message failed validation: " + "; ".join(problems)],
                     back_to_review=msg.sender_type == "AI_AUTO")
        if msg.sender_type == "AI_AUTO":
            from . import escalation

            escalation.open_or_update(db, conv, ["uncertain_inventory"], None)
        return out
    if msg.sender_type == "AI_AUTO":
        s = get_settings_row(db, conv.client_id)
        failed = rate_limits.acquire_all(db, [
            (f"auto:conv:{conv.id}", s.max_auto_replies_per_conversation_per_hour, 3600, 1),
            (f"auto:dealer:{conv.client_id}", s.max_auto_replies_per_dealership_per_hour, 3600, 1),
        ])
        if failed:
            return _block(db, msg, [f"auto-reply rate limit reached ({failed})"], back_to_review=True)
    resolved = customer_channel(db, conv.client_id, conv.channel)
    gate = send_gate.open_attempt(db, kind=KIND, ref_id=msg.id, channel=conv.channel,
                                  recipient=conv.external_conversation_id, live=resolved.live, client_id=conv.client_id)
    if not gate.allowed:
        return _block(db, msg, gate.reasons)
    db.commit()  # claim + ledger IN_FLIGHT are durable before the provider sees anything

    human_agent = (conv.channel == META_MESSENGER and msg.sender_type == "STAFF" and conv.last_inbound_at
                   and utcnow() - conv.last_inbound_at > timedelta(hours=24))
    result = resolved.adapter.send_text(conv.external_conversation_id, msg.body, human_agent=bool(human_agent))
    send_gate.close_attempt(db, gate.attempt, result)
    if result.ok:
        msg.delivery_status = result.status
        msg.provider_message_id = result.provider_message_id
        msg.delivery_error = None if resolved.live else f"simulated: {resolved.note}"
        conv.last_outbound_at = utcnow()
        conv.status = "AWAITING_CUSTOMER" if conv.status != "ESCALATED" else conv.status
        draft = _draft_for(db, msg)
        if draft and draft.status == "PENDING_REVIEW":
            draft.status = "SENT"
        record(db, "message.sent" if not result.simulated else "message.simulated", client_id=conv.client_id,
               entity_type="message", entity_id=msg.id, outcome="SIMULATED" if result.simulated else "OK",
               channel=conv.channel, sender_type=msg.sender_type, provider_message_id=result.provider_message_id)
        return {"status": msg.delivery_status}
    msg.delivery_error = result.error
    if result.uncertain:
        msg.delivery_status = "UNCERTAIN"
        _failure_alert(db, msg, uncertain=True)
        return {"uncertain": result.error}
    if conv.channel == TWILIO_SMS and "21610" in (result.error or ""):
        mark_opted_out(db, conv, source="provider: Twilio 21610 (recipient unsubscribed)")
        msg.delivery_status = "BLOCKED"
        return {"blocked": ["recipient unsubscribed at carrier level"]}
    if result.retryable:
        msg.delivery_status = "QUEUED"  # definitively not delivered → safe to retry
        db.commit()
        raise IntegrationError(result.error or "transient send failure")  # retryable: the job queue backs off
    msg.delivery_status = "FAILED"
    _failure_alert(db, msg)
    return {"failed": result.error}


def reconcile_uncertain(db: Session, msg: Message, *, delivered: bool, user_id: int) -> None:
    """Operator decision for an UNCERTAIN message after checking the provider/customer channel."""
    if msg.delivery_status != "UNCERTAIN":
        raise ValueError("message is not awaiting reconciliation")
    if delivered:
        msg.delivery_status = "SENT"
        msg.delivery_error = "marked delivered by operator after reconciliation"
    else:
        msg.delivery_status = "QUEUED"
        msg.delivery_error = None
        generation = 1 + len(db.scalars(select(SendAttempt.id).where(SendAttempt.kind == KIND,
                                                                      SendAttempt.ref_id == msg.id)).all())
        enqueue_send(db, msg, generation)
    record(db, "message.reconciled", client_id=msg.client_id, actor_id=user_id, entity_type="message", entity_id=msg.id,
           delivered=delivered)


def mark_opted_out(db: Session, conv: Conversation, *, source: str) -> None:
    if conv.opted_out:
        return
    conv.opted_out = True
    conv.opted_out_at = utcnow()
    if conv.contact_id:
        contact = db.get(CustomerContact, conv.contact_id)
        contact.opted_out_at = utcnow()
        if conv.channel == TWILIO_SMS:
            contact.sms_consent = False
    if not db.scalar(select(ContactSuppression.id).where(ContactSuppression.client_id == conv.client_id,
                                                         ContactSuppression.channel == conv.channel,
                                                         ContactSuppression.address == conv.external_conversation_id)):
        db.add(ContactSuppression(client_id=conv.client_id, channel=conv.channel, address=conv.external_conversation_id,
                                  reason="OPT_OUT", created_at=utcnow()))
    for d in db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id,
                                                    ResponseDraft.status == "PENDING_REVIEW")):
        d.status = "SUPERSEDED"
    for m in db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.delivery_status == "QUEUED",
                                              Message.sender_type.in_(["AI_AUTO", "SYSTEM"]))):
        m.delivery_status = "BLOCKED"
        m.delivery_error = "customer opted out"
    record(db, "conversation.opted_out", client_id=conv.client_id, entity_type="conversation", entity_id=conv.id,
           source=source)
