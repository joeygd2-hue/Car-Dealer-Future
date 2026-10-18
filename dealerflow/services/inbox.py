"""Importing replies from the authorized inbox and deciding next actions."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import audit, track_integration
from ..db import utcnow
from ..integrations.email import IncomingEmail, get_email_provider
from ..jobs import handler
from ..models import AgentState, Contact, InboundMessage, OutreachMessage, Prospect
from .classify import classify
from .compliance import suppress
from .crm import add_activity, advance_stage, create_task
from .normalize import domain_of, is_free_mail_domain

log = logging.getLogger("dealerflow.inbox")
POSITIVE = ("interested", "meeting_request", "question", "referral")


def match_prospect(db: Session, inc: IncomingEmail) -> tuple[Prospect | None, OutreachMessage | None, str | None]:
    """Match a reply to the outreach it answers. Strongest evidence first."""
    refs = " ".join(x for x in (inc.in_reply_to, inc.references) if x)
    if refs:
        for token in refs.replace(",", " ").split():
            m = db.scalar(select(OutreachMessage).where(OutreachMessage.rfc_message_id == token.strip()))
            if m:
                return m.prospect, m, "in_reply_to"
    if inc.thread_id:
        m = db.scalar(select(OutreachMessage).where(OutreachMessage.thread_id == inc.thread_id)
                      .order_by(OutreachMessage.sent_at.desc()))
        if m:
            return m.prospect, m, "thread"
    sender = inc.from_email.lower()
    m = db.scalar(select(OutreachMessage).where(OutreachMessage.to_email == sender, OutreachMessage.sent_at.is_not(None))
                  .order_by(OutreachMessage.sent_at.desc()))
    if m:
        return m.prospect, m, "recipient_email"
    c = db.scalar(select(Contact).where(Contact.email == sender))
    if c:
        return c.prospect, None, "contact_email"
    p = db.scalar(select(Prospect).where(Prospect.email == sender, Prospect.merged_into_id.is_(None)))
    if p:
        return p, None, "prospect_email"
    d = domain_of(sender)
    if d and not is_free_mail_domain(d):
        candidates = list(db.scalars(select(Prospect).where(Prospect.domain == d, Prospect.merged_into_id.is_(None))))
        if len(candidates) == 1:
            return candidates[0], None, "domain"
    return None, None, None


def ingest(db: Session, inc: IncomingEmail, *, provider: str) -> InboundMessage | None:
    """Store one inbound email (idempotent on provider id) and apply its consequences."""
    if db.scalar(select(InboundMessage.id).where(InboundMessage.provider == provider,
                                                 InboundMessage.provider_message_id == inc.provider_message_id)):
        return None
    from ..config import get_settings
    if inc.from_email == (get_settings().sender_email or "").lower():
        return None  # our own sent copy
    p, parent, matched_by = match_prospect(db, inc)
    c = classify(inc.subject, inc.body, inc.headers)
    row = InboundMessage(
        provider=provider, provider_message_id=inc.provider_message_id, rfc_message_id=inc.rfc_message_id,
        in_reply_to=inc.in_reply_to, thread_id=inc.thread_id, from_email=inc.from_email, subject=inc.subject[:500],
        body=inc.body, received_at=inc.received_at, prospect_id=p.id if p else None,
        outreach_message_id=parent.id if parent else None, matched_by=matched_by, classification=c.label,
        classification_confidence=c.confidence, classification_reasons=c.reasons + [f"also: {x}" for x in c.secondary],
        simulated=inc.simulated,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    apply_consequences(db, row)
    return row


def apply_consequences(db: Session, row: InboundMessage) -> None:
    """Deterministic, safe-direction actions. Replies that need judgement become tasks for a person."""
    p = row.prospect
    tag = " [SIMULATED]" if row.simulated else ""
    if row.classification == "opt_out":
        suppress(db, row.from_email, "reply_opt_out", source=f"inbound #{row.id}")
        if p:
            add_activity(db, p, "opt_out", f"Opt-out reply received{tag}", body=row.body[:2000], ref_type="inbound", ref_id=row.id)
        row.handled = True
        return
    if row.classification == "bounce":
        parent = db.get(OutreachMessage, row.outreach_message_id) if row.outreach_message_id else None
        if parent:
            parent.status = "bounced"
            suppress(db, parent.to_email, "bounce", source=f"inbound #{row.id}")
            add_activity(db, parent.prospect, "email_bounce", f"Bounce for {parent.to_email}{tag}", ref_type="inbound", ref_id=row.id)
        row.handled = True
        return
    if row.classification == "auto_reply":
        if p:
            add_activity(db, p, "email_in", f"Auto-reply received{tag}", body=row.body[:2000], ref_type="inbound", ref_id=row.id)
        row.handled = True
        return
    if p is None:
        row.escalated = True
        audit(db, "inbound.unmatched", entity_type="inbound", entity_id=row.id, data={"from": row.from_email})
        return
    from .sequences import stop_enrollments

    stop_enrollments(db, p, f"replied:{row.classification}")  # any human reply stops automated sequences
    if row.outreach_message_id:
        parent = db.get(OutreachMessage, row.outreach_message_id)
        if parent and parent.status in ("sent", "delivered"):
            parent.status, parent.replied_at = "replied", row.received_at
    add_activity(db, p, "email_in", f"Reply ({row.classification}){tag}: {row.subject}", body=row.body[:5000],
                 ref_type="inbound", ref_id=row.id,
                 data={"classification": row.classification, "confidence": row.classification_confidence,
                       "matched_by": row.matched_by})
    advance_stage(db, p, "replied", reason=f"Reply #{row.id}", )
    if row.classification in ("interested", "question", "referral"):
        advance_stage(db, p, "interested", reason=f"Reply #{row.id} classified {row.classification}")
    if row.classification == "meeting_request":
        advance_stage(db, p, "meeting_requested", reason=f"Reply #{row.classification} #{row.id}")
        from .meetings import open_request
        open_request(db, p, attendee_email=row.from_email, source=f"inbound #{row.id}")
    if row.classification == "not_interested":
        create_task(db, f"Reply from {p.name}: not interested — close as lost or follow up later?",
                    utcnow() + timedelta(days=1), prospect=p, kind="review", dedupe_key=f"reply:{row.id}")
    else:
        due = utcnow() + (timedelta(hours=4) if row.classification in POSITIVE else timedelta(days=1))
        create_task(db, f"Respond to {p.name} ({row.classification})", due, prospect=p, kind="follow_up",
                    dedupe_key=f"reply:{row.id}")
    if row.classification_confidence is not None and row.classification_confidence < 0.6:
        row.escalated = True


@handler("poll_inbox")
def poll_inbox_job(db: Session, payload: dict) -> dict:
    provider = get_email_provider()
    st = db.get(AgentState, 1)
    if st is None:
        st = AgentState(id=1, status="stopped")
        db.add(st)
    cp = dict(st.checkpoint or {})
    cursor_key = f"inbox_cursor:{provider.name}"
    with track_integration(db, provider.name, "fetch_inbound", simulated=provider.simulated) as ctx:
        items, cursor = provider.fetch_inbound(cp.get(cursor_key))
        ctx["detail"] = f"{len(items)} message(s)"
    stored = 0
    for inc in items:
        if ingest(db, inc, provider=provider.name):
            stored += 1
    cp[cursor_key] = cursor
    cp["inbox_polled_at"] = utcnow().isoformat()
    st.checkpoint = cp
    return {"fetched": len(items), "stored": stored}
