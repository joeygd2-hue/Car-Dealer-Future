"""Open/merge escalations and alert the right staff (ported from Phase 3)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import Conversation, ConversationEscalation, ConversationMessage, User
from . import notify
from .common import log
from .summaries import TOPIC_ACTIONS, build_summary, summary_text


def open_or_update(db: Session, conv: Conversation, reasons: list[str], trigger: ConversationMessage | None) -> ConversationEscalation:
    esc = db.scalar(select(ConversationEscalation).where(
        ConversationEscalation.client_id == conv.client_id, ConversationEscalation.conversation_id == conv.id,
        ConversationEscalation.status != "resolved"))
    new_reasons = list(reasons)
    if esc:
        new_reasons = [r for r in reasons if r not in esc.reasons]
        esc.reasons = list(esc.reasons) + new_reasons
    else:
        esc = ConversationEscalation(client_id=conv.client_id, conversation_id=conv.id, reasons=list(reasons),
                                     assigned_user_id=conv.assigned_user_id)
        db.add(esc)
    conv.escalation_status = "open"
    conv.status = "escalated"
    db.flush()
    summary = build_summary(db, conv)
    esc.summary = summary_text(summary)
    esc.recommended_action = next((TOPIC_ACTIONS[r] for r in esc.reasons if r in TOPIC_ACTIONS), summary["recommended_next_step"])
    if new_reasons:
        log(db, "escalation.opened" if len(new_reasons) == len(esc.reasons) else "escalation.updated", client_id=conv.client_id,
            entity_type="conversation", entity_id=conv.id, reasons=new_reasons, message_id=trigger.id if trigger else None)
        notify.notify(db, conv.client_id, kind="escalation",
                      subject=f"Needs attention: {', '.join(new_reasons)} (conversation #{conv.id})",
                      body=f"{esc.summary}\n\nRecommended: {esc.recommended_action}",
                      link=summary["conversation_link"], assigned_user_id=conv.assigned_user_id,
                      dedupe_key=f"esc:{esc.id}:{','.join(sorted(new_reasons))}")
    return esc


def resolve(db: Session, esc: ConversationEscalation, user: User) -> None:
    esc.status = "resolved"
    esc.resolved_by_id = user.id
    esc.resolved_at = utcnow()
    conv = db.get(Conversation, esc.conversation_id)
    conv.escalation_status = "resolved"
    if conv.status == "escalated":
        conv.status = "open"
    log(db, "escalation.resolved", client_id=esc.client_id, entity_type="conversation_escalation", entity_id=esc.id,
        actor_id=user.id, actor_type="user")
