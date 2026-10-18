"""Open/merge escalations and alert the right staff."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Conversation, Escalation, Message
from app.services import audit, notifications
from app.services.summaries import TOPIC_ACTIONS, build_summary, summary_text


def open_or_update(db: Session, conv: Conversation, reasons: list[str], trigger: Message | None) -> Escalation:
    esc = db.scalar(select(Escalation).where(Escalation.conversation_id == conv.id, Escalation.status != "RESOLVED"))
    new_reasons = list(reasons)
    if esc:
        new_reasons = [r for r in reasons if r not in esc.reasons]
        esc.reasons = list(esc.reasons) + new_reasons
    else:
        esc = Escalation(conversation_id=conv.id, dealership_id=conv.dealership_id, reasons=list(reasons),
                         summary="", recommended_action="", assigned_user_id=conv.assigned_user_id)
        db.add(esc)
    conv.escalation_status = "OPEN"
    conv.status = "ESCALATED"
    db.flush()
    summary = build_summary(db, conv)
    esc.summary = summary_text(summary)
    esc.recommended_action = next((TOPIC_ACTIONS[r] for r in esc.reasons if r in TOPIC_ACTIONS), summary["recommended_next_step"])
    if new_reasons:
        relevant = [m for m in conv.messages if m.direction == "INBOUND"][-3:]
        excerpt = "\n".join(f"> {m.body[:300]}" for m in relevant)
        audit.record(db, "escalation.opened" if len(new_reasons) == len(esc.reasons) else "escalation.updated",
                     actor_type="SYSTEM", dealership_id=conv.dealership_id, entity_type="conversation",
                     entity_id=conv.id, reasons=new_reasons, message_id=trigger.id if trigger else None)
        notifications.notify(
            db, conv.dealership_id, kind="ESCALATION",
            subject=f"Needs attention: {', '.join(new_reasons)} (conversation #{conv.id})",
            body=f"{esc.summary}\n\nRecent messages:\n{excerpt}\n\nRecommended: {esc.recommended_action}",
            link=summary["conversation_link"], assigned_user_id=conv.assigned_user_id,
            escalation_reasons=new_reasons, dedupe_key=f"esc:{esc.id}:{','.join(sorted(new_reasons))}")
    return esc


def resolve(db: Session, esc: Escalation, user_id: int) -> None:
    esc.status = "RESOLVED"
    esc.resolved_by_id = user_id
    esc.resolved_at = utcnow()
    conv = db.get(Conversation, esc.conversation_id)
    conv.escalation_status = "RESOLVED"
    if conv.status == "ESCALATED":
        conv.status = "OPEN"
    audit.record(db, "escalation.resolved", actor_type="USER", actor_user_id=user_id,
                 dealership_id=esc.dealership_id, entity_type="escalation", entity_id=esc.id)
