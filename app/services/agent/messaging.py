"""Drafting, approval, sending, inbound processing and meeting booking for the outreach agent."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from string import Formatter

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.calendar import CalendarError, get_calendar
from app.integrations.email import EmailDeliveryError, send_email
from app.models.base import utcnow
from app.models.core import User
from app.models.outreach import (
    CompanyFact,
    Enrollment,
    InboundMessage,
    Meeting,
    MessageTemplate,
    OutreachMessage,
    Prospect,
)
from app.services import audit
from app.services.agent import compliance
from app.services.agent.classify import ai_refine, classify, strip_quoted
from app.services.agent.state import get_config, log_activity

ALLOWED_PLACEHOLDERS = {"business_name", "city", "contact_name", "observation", "sender_company", "unsubscribe_url", "postal_address"}


class MessagingError(ValueError):
    pass


def validate_template(subject: str, body: str) -> None:
    for text in (subject, body):
        for _, name, _, _ in Formatter().parse(text):
            if name and name not in ALLOWED_PLACEHOLDERS:
                raise MessagingError(f"Unknown placeholder {{{name}}}. Allowed: {', '.join(sorted(ALLOWED_PLACEHOLDERS))}")


def unsubscribe_url(token: str) -> str:
    return f"{get_settings().base_url.rstrip('/')}/unsubscribe/{token}"


def compliance_footer(token: str, language: str = "en") -> str:
    s = get_settings()
    addr = s.company_postal_address.strip() or "[postal address not configured — sandbox only]"
    if language == "es":
        return f"\n\n--\n{s.company_name}, {addr}\nSi no desea recibir más correos, haga clic aquí: {unsubscribe_url(token)}"
    return f"\n\n--\n{s.company_name}, {addr}\nTo stop receiving these emails, click: {unsubscribe_url(token)}"


def render_template(t: MessageTemplate, p: Prospect, token: str) -> tuple[str, str]:
    """Render only from verified prospect fields and evidence; refuse if a needed fact is missing."""
    s = get_settings()
    obs = next((e["fact"] for e in p.evidence if e.get("code") and e.get("method") == "measured"), None)
    values = {
        "business_name": p.business_name, "city": p.city, "contact_name": p.contact_name, "observation": obs,
        "sender_company": s.company_name, "unsubscribe_url": unsubscribe_url(token), "postal_address": s.company_postal_address,
    }
    needed = {n for text in (t.subject, t.body) for _, n, _, _ in Formatter().parse(text) if n}
    missing = [n for n in needed if not values.get(n)]
    if missing:
        raise MessagingError(f"Missing verified facts for placeholders: {', '.join(missing)}")
    subject = t.subject.format(**values)
    body = t.body.format(**values)
    if "/unsubscribe/" not in body:
        body += compliance_footer(token, t.language)
    elif s.company_postal_address and s.company_postal_address not in body:
        body += f"\n{s.company_name}, {s.company_postal_address}"
    return subject, body


def _new_token() -> str:
    return secrets.token_urlsafe(18)


def draft_step(db: Session, enr: Enrollment) -> OutreachMessage | None:
    """Create the message for the enrollment's next step (idempotent per enrollment+step)."""
    key = f"enr:{enr.id}:step:{enr.next_step}"
    existing = db.scalar(select(OutreachMessage).where(OutreachMessage.idempotency_key == key))
    if existing:
        return existing
    steps = enr.campaign.steps
    if enr.next_step >= len(steps):
        enr.status, enr.stop_reason, enr.stopped_at = "completed", "sequence finished", utcnow()
        return None
    tpl = db.get(MessageTemplate, steps[enr.next_step]["template_id"])
    p = enr.prospect
    if tpl is None or not tpl.approved:
        raise MessagingError("Template missing or not approved")
    token = _new_token()
    subject, body = render_template(tpl, p, token)
    auto_ok = enr.campaign.auto_send_approved_templates and enr.campaign.auto_send_authorized_by_user_id is not None
    msg = OutreachMessage(
        prospect_id=p.id, enrollment_id=enr.id, step_index=enr.next_step, kind="initial" if enr.next_step == 0 else "followup",
        channel="email", to_address=p.email, subject=subject, body=body, template_id=tpl.id, generated_by="template",
        status="approved" if auto_ok else "pending_approval",
        requires_approval_reason=None if auto_ok else "campaign requires per-message approval",
        reply_token=token, idempotency_key=key,
    )
    db.add(msg)
    db.flush()
    log_activity(db, "message.drafted", "ok", prospect_id=p.id, campaign_id=enr.campaign_id, data={"message_id": msg.id, "status": msg.status})
    return msg


def approve_message(db: Session, msg: OutreachMessage, actor: User, subject: str | None = None, body: str | None = None) -> None:
    if msg.status not in ("pending_approval", "draft", "blocked"):
        raise MessagingError(f"Message is {msg.status}")
    if subject is not None:
        msg.subject = subject
    if body is not None:
        if "/unsubscribe/" not in body:
            body += compliance_footer(msg.reply_token)
        msg.body = body
        msg.generated_by = "human" if msg.generated_by == "template" else msg.generated_by
    msg.status, msg.approved_by_user_id, msg.approved_at, msg.blocked_reason = "approved", actor.id, utcnow(), None
    log_activity(db, "message.approved", "ok", prospect_id=msg.prospect_id, data={"message_id": msg.id, "by": actor.email})
    audit.record(db, "outreach.approved", actor=actor, entity=msg)


def reject_message(db: Session, msg: OutreachMessage, actor: User, reason: str) -> None:
    msg.status, msg.blocked_reason = "cancelled", f"rejected: {reason}"[:200]
    log_activity(db, "message.rejected", "ok", prospect_id=msg.prospect_id, data={"message_id": msg.id, "reason": reason})
    audit.record(db, "outreach.rejected", actor=actor, entity=msg, details={"reason": reason})


def send_message(db: Session, msg: OutreachMessage, job_id: int | None = None) -> str:
    """Gate-check and send one approved message. Returns the outcome string."""
    cfg = get_config(db)
    gate = compliance.check_send(db, cfg, msg)
    if not gate.ok:
        if not gate.retry_later:
            msg.status, msg.blocked_reason = "blocked", gate.reason
        log_activity(db, "message.send", "blocked", prospect_id=msg.prospect_id, job_id=job_id, detail=gate.reason, data={"message_id": msg.id, "retry_later": gate.retry_later})
        return "deferred" if gate.retry_later else "blocked"
    # Claim atomically so two workers can never send the same message.
    claimed = db.execute(update(OutreachMessage).where(OutreachMessage.id == msg.id, OutreachMessage.status == "approved").values(status="queued")).rowcount
    db.commit()
    if claimed != 1:
        return "already_claimed"
    db.refresh(msg)
    s = get_settings()
    headers = {
        "List-Unsubscribe": f"<{unsubscribe_url(msg.reply_token)}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "X-Outreach-Token": msg.reply_token,
    }
    if msg.kind in ("followup", "reply"):
        prev = db.scalar(
            select(OutreachMessage).where(OutreachMessage.prospect_id == msg.prospect_id, OutreachMessage.status == "sent")
            .order_by(OutreachMessage.sent_at.desc()).limit(1)
        )
        if prev and prev.provider_message_id:
            headers["In-Reply-To"] = prev.provider_message_id
            headers["References"] = prev.provider_message_id
    msg.attempts += 1
    try:
        email = send_email(
            db, purpose="outreach", to=msg.to_address, subject=msg.subject, body_text=msg.body, extra_headers=headers,
            reply_to=s.company_reply_to or None, idempotency_key=f"outreach:{msg.idempotency_key}", allow_live=cfg.live_mode,
        )
    except EmailDeliveryError as exc:
        msg.last_error = str(exc)[:2000]
        msg.status = "failed" if msg.attempts >= cfg.max_retries else "approved"
        log_activity(db, "message.send", "error", prospect_id=msg.prospect_id, job_id=job_id, detail=str(exc), data={"message_id": msg.id, "attempt": msg.attempts})
        return "error"
    msg.status, msg.sent_at, msg.provider, msg.provider_message_id = "sent", utcnow(), email.provider, email.provider_message_id
    p = db.get(Prospect, msg.prospect_id)
    p.last_contacted_at = msg.sent_at
    if p.stage == "qualified":
        p.stage = "contacted"
    if msg.enrollment_id and msg.kind != "reply":
        enr = db.get(Enrollment, msg.enrollment_id)
        enr.next_step += 1
        if enr.next_step >= len(enr.campaign.steps):
            enr.status, enr.stop_reason, enr.stopped_at, enr.next_action_at = "completed", "sequence finished", utcnow(), None
        else:
            enr.next_action_at = utcnow() + timedelta(days=int(enr.campaign.steps[enr.next_step].get("delay_days", 4)))
    log_activity(db, "message.send", "ok", prospect_id=msg.prospect_id, job_id=job_id, data={"message_id": msg.id, "provider": email.provider, "delivery": email.status})
    return email.status


# ------------------------------------------------------------------ inbound --


def ingest_inbound(db: Session, *, provider: str, provider_message_id: str, from_address: str, subject: str | None, body: str,
                   in_reply_to: str | None = None, to_addresses: list[str] | None = None, headers: dict | None = None) -> tuple[InboundMessage, bool]:
    """Store an inbound email once (dedupe on provider message id) and associate it with a prospect."""
    existing = db.scalar(select(InboundMessage).where(InboundMessage.provider_message_id == provider_message_id))
    if existing:
        return existing, False
    msg = InboundMessage(provider=provider, provider_message_id=provider_message_id, in_reply_to=in_reply_to,
                         from_address=from_address.lower().strip(), subject=subject, body=body[:50000])
    original = None
    if in_reply_to:
        original = db.scalar(select(OutreachMessage).where(OutreachMessage.provider_message_id == in_reply_to.strip()))
        if original:
            msg.match_method = "in_reply_to"
    if original is None:
        tokens = re.findall(r"/unsubscribe/([A-Za-z0-9_-]{20,})", body) + [
            m for addr in (to_addresses or []) for m in re.findall(r"\+([A-Za-z0-9_-]{20,})@", addr)
        ]
        for tok in tokens:
            original = db.scalar(select(OutreachMessage).where(OutreachMessage.reply_token == tok))
            if original:
                msg.match_method = "token"
                break
    if original is not None:
        msg.outreach_message_id, msg.prospect_id = original.id, original.prospect_id
    else:
        p = db.scalar(select(Prospect).where(Prospect.email == msg.from_address))
        if p:
            msg.prospect_id, msg.match_method = p.id, "from_address"
    cls = classify(msg.from_address, subject, body, headers)
    if cls.label == "unknown":
        cls = ai_refine(db, strip_quoted(body), cls)
    msg.classification, msg.classification_method = cls.label, cls.method
    msg.classification_confidence, msg.needs_human = cls.confidence, cls.needs_human or msg.prospect_id is None
    db.add(msg)
    db.flush()
    log_activity(db, "inbound.received", "ok", prospect_id=msg.prospect_id, data={"inbound_id": msg.id, "classification": cls.label, "match": msg.match_method})
    return msg, True


def stop_sequences(db: Session, prospect_id: int, reason: str) -> int:
    n = 0
    for enr in db.scalars(select(Enrollment).where(Enrollment.prospect_id == prospect_id, Enrollment.status == "active")):
        enr.status, enr.stop_reason, enr.stopped_at, enr.next_action_at = "stopped", reason, utcnow(), None
        n += 1
    db.execute(
        update(OutreachMessage)
        .where(OutreachMessage.prospect_id == prospect_id, OutreachMessage.kind != "reply",
               OutreachMessage.status.in_(("draft", "pending_approval", "approved")))
        .values(status="cancelled", blocked_reason=f"sequence stopped: {reason}")
    )
    return n


def _set_stage(db: Session, p: Prospect, stage: str, why: str) -> None:
    if p.stage != stage:
        log_activity(db, "prospect.stage", "ok", prospect_id=p.id, from_state=p.stage, to_state=stage, detail=why)
        p.stage = stage


def handle_inbound(db: Session, msg: InboundMessage) -> str:
    """Decide and take the next action for a classified inbound message."""
    if msg.handled_at:
        return msg.next_action or "already_handled"
    p = db.get(Prospect, msg.prospect_id) if msg.prospect_id else None
    label = msg.classification
    action = "human_review"
    if label == "bounce":
        target = (p.email if p else None)
        if target:
            compliance.add_suppression(db, "email", target, "bounce", f"inbound:{msg.id}")
            stop_sequences(db, p.id, "bounced")
            p.email = None
        action = "suppressed_bounce"
    elif p is None:
        action = "human_review_unmatched"
    elif label == "opt_out":
        compliance.add_suppression(db, "email", msg.from_address, "opt_out", f"inbound:{msg.id}")
        if p.email and p.email != msg.from_address:
            compliance.add_suppression(db, "email", p.email, "opt_out", f"inbound:{msg.id}")
        stop_sequences(db, p.id, "opted out")
        _set_stage(db, p, "opted_out", "reply requested no contact")
        action = "opted_out"
    elif label == "out_of_office":
        for enr in db.scalars(select(Enrollment).where(Enrollment.prospect_id == p.id, Enrollment.status == "active")):
            enr.next_action_at = max(enr.next_action_at or utcnow(), utcnow()) + timedelta(days=7)
        action = "followup_delayed"
    else:
        # Any genuine reply stops the automated sequence.
        stop_sequences(db, p.id, "prospect replied")
        p.replied_at = p.replied_at or msg.received_at
        if label == "not_interested":
            _set_stage(db, p, "not_interested", "declined")
            action = "closed_not_interested"
        elif label == "sensitive" or msg.needs_human:
            _set_stage(db, p, "replied", "needs human")
            action = "escalated"
        elif label in ("meeting_request", "positive"):
            _set_stage(db, p, "interested", label)
            chosen = _chosen_slot(db, msg, p)
            if chosen is not None:
                action = book_meeting(db, p, chosen, booked_by="agent", reply_to=msg)
            else:
                action = propose_meeting(db, p, msg)
        elif label == "question":
            _set_stage(db, p, "replied", "asked a question")
            action = answer_question(db, p, msg)
        else:
            _set_stage(db, p, "replied", "unclassified reply")
            action = "escalated"
    msg.handled_at, msg.next_action = utcnow(), action
    if action.startswith(("escalated", "human_review")):
        msg.needs_human = True
    log_activity(db, "inbound.handled", "ok", prospect_id=msg.prospect_id, data={"inbound_id": msg.id, "action": action})
    return action


def _reply(db: Session, p: Prospect, inbound: InboundMessage, subject: str, body: str, *, auto: bool, reason: str, extra: dict | None = None) -> OutreachMessage:
    token = _new_token()
    m = OutreachMessage(
        prospect_id=p.id, kind="reply", channel="email", to_address=inbound.from_address,
        subject=subject if subject.lower().startswith("re:") else f"Re: {subject}", body=body + compliance_footer(token),
        generated_by="template", status="approved" if auto else "pending_approval",
        requires_approval_reason=None if auto else reason, reply_token=token,
        idempotency_key=f"reply:{inbound.id}:{(extra or {}).get('kind', 'r')}", in_reply_to_inbound_id=inbound.id,
    )
    db.add(m)
    db.flush()
    if extra and extra.get("slots"):
        log_activity(db, "meeting.proposed", "ok", prospect_id=p.id, data={"message_id": m.id, "slots": extra["slots"]})
    return m


def _auto_reply_allowed(db: Session) -> bool:
    cfg = get_config(db)
    from app.models.outreach import Campaign

    return cfg.state == "running" and db.scalar(
        select(Campaign.id).where(Campaign.status == "active", Campaign.auto_send_approved_templates.is_(True),
                                  Campaign.auto_send_authorized_by_user_id.is_not(None))
    ) is not None


def answer_question(db: Session, p: Prospect, inbound: InboundMessage) -> str:
    text = strip_quoted(inbound.body).lower()
    facts = db.scalars(select(CompanyFact).where(CompanyFact.approved.is_(True))).all()
    best, hits = None, 0
    for f in facts:
        n = sum(1 for k in f.keywords if k.lower() in text)
        if n > hits:
            best, hits = f, n
    if best is None or hits == 0:
        return "escalated_no_approved_answer"
    from app.services.agent.classify import detect_language

    lang = detect_language(text)
    answer = best.answer_es if lang == "es" and best.answer_es else best.answer_en
    auto = best.auto_reply_allowed and hits >= 1 and _auto_reply_allowed(db)
    _reply(db, p, inbound, inbound.subject or "Your question", answer, auto=auto, reason="answer from approved company facts — confirm", extra={"kind": "answer"})
    return "answered_auto" if auto else "answer_pending_approval"


def propose_meeting(db: Session, p: Prospect, inbound: InboundMessage) -> str:
    cfg = get_config(db)
    try:
        cal = get_calendar(cfg.live_mode)
        slots = cal.free_slots(utcnow(), 3, 30, cfg.timezone)
    except CalendarError as exc:
        log_activity(db, "meeting.propose", "error", prospect_id=p.id, detail=str(exc))
        return "escalated_calendar_error"
    slots = [s for s in slots if not _slot_taken(db, s)]
    from zoneinfo import ZoneInfo

    z = ZoneInfo(cfg.timezone)
    lines = [f"  Option {i}: {s.replace(tzinfo=ZoneInfo('UTC')).astimezone(z):%A %B %d at %I:%M %p} ({cfg.timezone})" for i, s in enumerate(slots, start=1)]
    body = "Thanks for your reply. Here are a few times for a 30-minute video call:\n" + "\n".join(lines) + "\n\nReply with the option number that works best."
    m = _reply(db, p, inbound, inbound.subject or "Meeting times", body, auto=_auto_reply_allowed(db), reason="meeting proposal", extra={"kind": "slots", "slots": [s.isoformat() for s in slots]})
    return "meeting_slots_proposed" if m.status == "approved" else "meeting_slots_pending_approval"


def _slot_taken(db: Session, start: datetime) -> bool:
    return db.scalar(select(Meeting.id).where(Meeting.starts_at == start, Meeting.status == "booked")) is not None


def _chosen_slot(db: Session, inbound: InboundMessage, p: Prospect) -> datetime | None:
    """If the reply picks an option from slots we proposed (and sent), return that slot."""
    m = re.search(r"\b(?:option|opción|#)\s*([1-3])\b|^\s*([1-3])\s*$", strip_quoted(inbound.body), re.I | re.M)
    if not m:
        return None
    idx = int(m.group(1) or m.group(2)) - 1
    from app.models.outreach import AgentActivity

    proposals = db.scalars(
        select(AgentActivity).where(AgentActivity.prospect_id == p.id, AgentActivity.action == "meeting.proposed").order_by(AgentActivity.id.desc())
    ).all()
    for act in proposals:
        sent = db.get(OutreachMessage, act.data.get("message_id"))
        if sent and sent.status == "sent":
            slots = act.data.get("slots", [])
            if 0 <= idx < len(slots):
                return datetime.fromisoformat(slots[idx])
    return None


def book_meeting(db: Session, p: Prospect, start: datetime, *, booked_by: str, reply_to: InboundMessage | None = None, minutes: int = 30) -> str:
    cfg = get_config(db)
    if not p.email:
        return "escalated_no_email"
    if _slot_taken(db, start):
        return propose_meeting(db, p, reply_to) if reply_to else "slot_taken"
    try:
        ev = get_calendar(cfg.live_mode).book(start, minutes, f"{get_settings().company_name} × {p.business_name}", p.email, "Intro call booked via outreach")
    except CalendarError as exc:
        log_activity(db, "meeting.book", "error", prospect_id=p.id, detail=str(exc))
        return "escalated_calendar_error"
    meeting = Meeting(prospect_id=p.id, starts_at=ev.starts_at, ends_at=ev.ends_at, provider=ev.provider, provider_event_id=ev.event_id, video_url=ev.video_url, booked_by=booked_by)
    db.add(meeting)
    stop_sequences(db, p.id, "meeting booked")
    _set_stage(db, p, "meeting_booked", "meeting booked")
    log_activity(db, "meeting.booked", "ok", prospect_id=p.id, data={"provider": ev.provider, "event_id": ev.event_id, "starts_at": ev.starts_at.isoformat()})
    if reply_to is not None:
        from zoneinfo import ZoneInfo

        local = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(cfg.timezone))
        _reply(db, p, reply_to, reply_to.subject or "Meeting confirmed", f"You're booked for {local:%A %B %d at %I:%M %p} ({cfg.timezone}).\nVideo link: {ev.video_url}",
               auto=_auto_reply_allowed(db), reason="meeting confirmation", extra={"kind": "confirm"})
    return "meeting_booked"


def unsubscribe(db: Session, token: str, source: str = "link") -> bool:
    msg = db.scalar(select(OutreachMessage).where(OutreachMessage.reply_token == token))
    if msg is None:
        return False
    p = db.get(Prospect, msg.prospect_id)
    if msg.to_address:
        compliance.add_suppression(db, "email", msg.to_address, "opt_out", f"unsubscribe:{source}")
    stop_sequences(db, p.id, "opted out")
    _set_stage(db, p, "opted_out", f"unsubscribe {source}")
    log_activity(db, "prospect.unsubscribed", "ok", prospect_id=p.id, data={"source": source})
    return True
