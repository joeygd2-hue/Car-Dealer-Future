"""Outbound customer messages: queued as jobs, stop conditions re-checked at send time."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.db import utcnow
from app.integrations import registry
from app.models import ContactSuppression, Conversation, CustomerContact, Dealership, Message, ResponseDraft
from app.services import audit, controls, jobs, notifications
from app.services.automation_policy import automation_blockers, channel_connection, get_settings_row


def queue_outbound(db: Session, conv: Conversation, body: str, *, sender_type: str, user_id: int | None = None,
                   draft: ResponseDraft | None = None, idempotency_key: str | None = None) -> Message:
    msg = Message(conversation_id=conv.id, dealership_id=conv.dealership_id, channel=conv.channel, direction="OUTBOUND",
                  sender_type=sender_type, sender_user_id=user_id, body=body, language=conv.preferred_language,
                  delivery_status="QUEUED", idempotency_key=idempotency_key)
    db.add(msg)
    db.flush()
    if draft is not None:
        draft.sent_message_id = msg.id
    jobs.enqueue(db, "outbound.send", {"message_id": msg.id}, idempotency_key=f"outbound:{msg.id}", max_attempts=5,
                 priority=50)
    audit.record(db, "message.queued", actor_type="USER" if user_id else "SYSTEM", actor_user_id=user_id,
                 dealership_id=conv.dealership_id, entity_type="message", entity_id=msg.id, sender_type=sender_type)
    return msg


def record_manual_external(db: Session, conv: Conversation, body: str, *, user_id: int,
                           draft: ResponseDraft | None = None) -> Message:
    """Staff sent this reply themselves outside the platform (e.g. pasted into Marketplace)."""
    msg = Message(conversation_id=conv.id, dealership_id=conv.dealership_id, channel=conv.channel, direction="OUTBOUND",
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
    audit.record(db, "message.manual_external_recorded", actor_type="USER", actor_user_id=user_id,
                 dealership_id=conv.dealership_id, entity_type="message", entity_id=msg.id, channel=conv.channel)
    return msg


def _send_blockers(db: Session, msg: Message, conv: Conversation) -> list[str]:
    reasons = []
    if conv.opted_out:
        reasons.append("customer opted out")
    contact = db.get(CustomerContact, conv.contact_id) if conv.contact_id else None
    if db.query(ContactSuppression.id).filter_by(dealership_id=conv.dealership_id, channel=conv.channel,
                                                 address=conv.external_conversation_id).first():
        reasons.append("recipient on opt-out list")
    if msg.sender_type == "AI_AUTO":
        reasons += automation_blockers(db, conv, get_settings_row(db, conv.dealership_id))
    elif msg.sender_type == "SYSTEM":
        if controls.get_flag(db, controls.GLOBAL_AUTOMATION_PAUSE):
            reasons.append("global automation pause is on")
        if get_settings_row(db, conv.dealership_id).paused:
            reasons.append("dealership automation is paused")
        if conv.channel == registry.TWILIO_SMS and not (contact and contact.sms_consent):
            reasons.append("no recorded SMS consent for automated message")
    if conv.channel == registry.META_MESSENGER:
        last_in = conv.last_inbound_at
        age = (utcnow() - last_in) if last_in else timedelta(days=999)
        if age > timedelta(days=7):
            reasons.append("outside Messenger 7-day human-agent window")
        elif age > timedelta(hours=24) and msg.sender_type != "STAFF":
            reasons.append("outside Messenger 24-hour window (only staff may reply with HUMAN_AGENT tag)")
    return list(dict.fromkeys(reasons))


def _on_dead(db: Session, payload: dict, job) -> None:
    msg = db.get(Message, payload.get("message_id"))
    if msg and msg.delivery_status in ("QUEUED", "SENDING"):
        msg.delivery_status = "FAILED"
        msg.delivery_error = (job.last_error or "")[:1000]
        _failure_alert(db, msg)


def _failure_alert(db: Session, msg: Message) -> None:
    conv = db.get(Conversation, msg.conversation_id)
    draft = db.query(ResponseDraft).filter_by(sent_message_id=msg.id).first()
    if draft:
        draft.status = "SEND_FAILED"
    audit.record(db, "message.send_failed", actor_type="WORKER", dealership_id=msg.dealership_id, entity_type="message",
                 entity_id=msg.id, outcome="FAILED", external=True, error=msg.delivery_error)
    notifications.notify(db, msg.dealership_id, kind="SYSTEM", subject=f"Message failed to send (conversation #{conv.id})",
                         body=f"A reply on {conv.channel} could not be delivered: {msg.delivery_error}\n\nPlease follow up manually.",
                         link=f"/portal/{conv.dealership_id}/conversations/{conv.id}", assigned_user_id=conv.assigned_user_id,
                         dedupe_key=f"sendfail:{msg.id}")


@jobs.handler("outbound.send", on_dead=_on_dead)
def send_job(db: Session, payload: dict, job) -> dict:
    msg = db.get(Message, jobs.payload_get(payload, "message_id"))
    if msg is None or msg.delivery_status not in ("QUEUED", "SENDING"):
        return {"skipped": True}
    conv = db.get(Conversation, msg.conversation_id)
    blockers = _send_blockers(db, msg, conv)
    if blockers:
        msg.delivery_status = "BLOCKED"
        msg.delivery_error = "; ".join(blockers)
        draft = db.query(ResponseDraft).filter_by(sent_message_id=msg.id).first()
        if draft:
            draft.status = "BLOCKED"
        audit.record(db, "message.blocked", actor_type="WORKER", dealership_id=conv.dealership_id, entity_type="message",
                     entity_id=msg.id, outcome="BLOCKED", reasons=blockers)
        return {"blocked": blockers}
    conn = channel_connection(db, conv.dealership_id, conv.channel)
    resolved = registry.customer_channel(db, conn, conv.channel)
    msg.delivery_status = "SENDING"
    human_agent = (conv.channel == registry.META_MESSENGER and msg.sender_type == "STAFF" and conv.last_inbound_at
                   and utcnow() - conv.last_inbound_at > timedelta(hours=24))
    result = resolved.adapter.send_text(conv.external_conversation_id, msg.body, human_agent=bool(human_agent))
    if result.ok:
        msg.delivery_status = result.status
        msg.provider_message_id = result.provider_message_id
        msg.delivery_error = None if resolved.live else f"simulated: {resolved.note}"
        conv.last_outbound_at = utcnow()
        conv.status = "AWAITING_CUSTOMER" if conv.status != "ESCALATED" else conv.status
        draft = db.query(ResponseDraft).filter_by(sent_message_id=msg.id).first()
        if draft and draft.status == "PENDING_REVIEW":
            draft.status = "SENT"
        audit.record(db, "message.sent" if not result.simulated else "message.simulated", actor_type="WORKER",
                     dealership_id=conv.dealership_id, entity_type="message", entity_id=msg.id,
                     outcome="SIMULATED" if result.simulated else "OK", external=not result.simulated,
                     channel=conv.channel, sender_type=msg.sender_type, provider_message_id=result.provider_message_id)
        return {"status": msg.delivery_status}
    msg.delivery_error = result.error
    if conv.channel == registry.TWILIO_SMS and "21610" in (result.error or ""):
        mark_opted_out(db, conv, source="provider: Twilio 21610 (recipient unsubscribed)")
        msg.delivery_status = "BLOCKED"
        return {"blocked": ["recipient unsubscribed at carrier level"]}
    if result.retryable:
        raise RuntimeError(result.error or "transient send failure")
    msg.delivery_status = "FAILED"
    _failure_alert(db, msg)
    return {"failed": result.error}


def mark_opted_out(db: Session, conv: Conversation, *, source: str) -> None:
    if conv.opted_out:
        return
    conv.opted_out = True
    conv.opted_out_at = utcnow()
    if conv.contact_id:
        contact = db.get(CustomerContact, conv.contact_id)
        contact.opted_out_at = utcnow()
        if conv.channel == registry.TWILIO_SMS:
            contact.sms_consent = False
    if not db.query(ContactSuppression.id).filter_by(dealership_id=conv.dealership_id, channel=conv.channel,
                                                     address=conv.external_conversation_id).first():
        db.add(ContactSuppression(dealership_id=conv.dealership_id, channel=conv.channel,
                                  address=conv.external_conversation_id, reason="OPT_OUT", created_at=utcnow()))
    for d in db.query(ResponseDraft).filter_by(conversation_id=conv.id, status="PENDING_REVIEW"):
        d.status = "SUPERSEDED"
    for m in db.query(Message).filter(Message.conversation_id == conv.id, Message.delivery_status == "QUEUED",
                                      Message.sender_type.in_(["AI_AUTO", "SYSTEM"])):
        m.delivery_status = "BLOCKED"
        m.delivery_error = "customer opted out"
    dealership = db.get(Dealership, conv.dealership_id)
    audit.record(db, "conversation.opted_out", actor_type="CUSTOMER", dealership_id=conv.dealership_id,
                 entity_type="conversation", entity_id=conv.id, source=source, dealership=dealership.name)
