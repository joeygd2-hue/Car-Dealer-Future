"""Pre-send compliance gate. Every outbound email goes through ``email_blockers`` twice:
once at approval and again immediately before the provider call.

Rules implemented (see docs/research/feasibility-report.md for sources):
- CAN-SPAM: sender identity, non-deceptive subject, ad disclosure, valid physical postal address,
  working opt-out link honoured immediately (law allows 10 business days; we do it at once).
- Internal suppression list (emails, domains) and do-not-contact flag.
- Stop conditions: a reply, a booked meeting or an opt-out ends automated sequences.
- Volume limits: per-prospect touch cap and cooldown, global daily cap, per-campaign caps.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..db import utcnow
from ..models import InboundMessage, Meeting, OutreachMessage, Prospect, Suppression
from .normalize import domain_of, normalize_email, normalize_phone, valid_email

SENT_STATES = ("sending", "sent", "delivered", "replied", "bounced")
DECEPTIVE_SUBJECT = re.compile(r"^\s*(re|fw|fwd)\s*:", re.I)


def is_suppressed(db: Session, email: str | None = None, phone: str | None = None) -> Suppression | None:
    vals = []
    if email:
        e = email.strip().lower()
        vals += [e, "@" + (domain_of(e) or "")]
    if phone:
        p = normalize_phone(phone)
        if p:
            vals.append(p)
    if not vals:
        return None
    return db.scalar(select(Suppression).where(Suppression.value.in_(vals)))


def suppress(db: Session, value: str, reason: str, *, source: str = "", actor_id: int | None = None,
             kind: str | None = None) -> Suppression:
    """Add to suppression list and mark matching prospects do-not-contact. Idempotent."""
    v = value.strip().lower()
    if kind is None:
        kind = "domain" if v.startswith("@") else ("phone" if normalize_phone(v) and "@" not in v else "email")
    if kind == "phone":
        v = normalize_phone(v) or v
    existing = db.scalar(select(Suppression).where(Suppression.value == v))
    if existing:
        return existing
    row = Suppression(value=v, kind=kind, reason=reason, source=source[:255])
    db.add(row)
    db.flush()
    _apply_to_prospects(db, row, actor_id)
    audit(db, "suppression.add", actor_id=actor_id, entity_type="suppression", entity_id=row.id,
          data={"value": v, "reason": reason, "source": source})
    # Cancel anything not yet sent to this address.
    q = select(OutreachMessage).where(OutreachMessage.status.in_(("draft", "approved", "queued")))
    q = q.where(OutreachMessage.to_email.ilike(f"%{v}") if kind == "domain" else OutreachMessage.to_email == v)
    for m in db.scalars(q):
        m.status, m.error = "cancelled", f"suppressed: {reason}"
    return row


def _apply_to_prospects(db: Session, row: Suppression, actor_id: int | None) -> None:
    from .crm import set_stage
    from .sequences import stop_enrollments

    if row.kind == "email":
        # Conservative default: an opt-out from anyone at a dealership stops outreach to that dealership.
        from ..models import Contact
        pids = set(db.scalars(select(Contact.prospect_id).where(Contact.email == row.value)))
        pids |= set(db.scalars(select(Prospect.id).where(Prospect.email == row.value)))
        pids |= set(db.scalars(select(OutreachMessage.prospect_id).where(OutreachMessage.to_email == row.value)))
        targets = list(db.scalars(select(Prospect).where(Prospect.id.in_(pids)))) if pids else []
    elif row.kind == "domain":
        targets = list(db.scalars(select(Prospect).where(Prospect.domain == row.value.lstrip("@"))))
    else:
        targets = list(db.scalars(select(Prospect).where(Prospect.phone_e164 == row.value)))
    for p in targets:
        stop_enrollments(db, p, f"suppressed:{row.reason}")
        if not p.do_not_contact:
            set_stage(db, p, "do_not_contact", reason=f"Suppressed ({row.reason}) {row.source}", workflow=True)


def sent_today(db: Session) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(OutreachMessage).where(
        OutreachMessage.sent_at >= start, OutreachMessage.simulated.is_(False))) or 0


def email_blockers(db: Session, msg: OutreachMessage, *, at_send: bool = False) -> list[str]:
    """Return reasons this message must not be sent (empty list = allowed)."""
    s = get_settings()
    p: Prospect = msg.prospect or db.get(Prospect, msg.prospect_id)
    reasons: list[str] = []
    if not (s.business_name and s.business_postal_address and s.sender_email):
        reasons.append("Sender identity incomplete: set DF_BUSINESS_NAME, DF_BUSINESS_POSTAL_ADDRESS and DF_SENDER_EMAIL (CAN-SPAM)")
    to = normalize_email(msg.to_email)
    if not to or not valid_email(to):
        reasons.append(f"Invalid recipient address {msg.to_email!r}")
    if p is None or p.merged_into_id:
        reasons.append("Prospect missing or merged into another record")
        return reasons
    if p.do_not_contact or p.stage == "do_not_contact":
        reasons.append("Prospect is marked do-not-contact")
    sup = is_suppressed(db, to)
    if sup:
        reasons.append(f"Recipient is on the suppression list ({sup.reason})")
    if DECEPTIVE_SUBJECT.match(msg.subject or "") and not msg.in_reply_to_inbound_id:
        reasons.append("Subject starts with RE:/FW: but the message is not a reply (deceptive subject)")
    if "{{UNSUBSCRIBE_URL}}" in msg.body or "{" in (msg.subject or ""):
        reasons.append("Message still contains unfilled template placeholders")

    is_reply = msg.in_reply_to_inbound_id is not None
    if not is_reply:
        # Stop conditions for cold / sequence outreach
        replied = db.scalar(select(func.count()).select_from(InboundMessage).where(
            InboundMessage.prospect_id == p.id, InboundMessage.classification.notin_(("auto_reply", "bounce"))))
        if replied:
            reasons.append("Prospect has replied; automated/cold outreach stops (answer the reply instead)")
        booked = db.scalar(select(func.count()).select_from(Meeting).where(
            Meeting.prospect_id == p.id, Meeting.status.in_(("booked", "rescheduled", "completed"))))
        if booked:
            reasons.append("Prospect has a meeting booked; sequence stops")
        prior = list(db.scalars(select(OutreachMessage).where(
            OutreachMessage.prospect_id == p.id, OutreachMessage.id != msg.id,
            OutreachMessage.status.in_(SENT_STATES))))
        if len(prior) >= s.outreach_max_touches_per_prospect:
            reasons.append(f"Touch limit reached ({len(prior)} messages already sent)")
        last = max((m.sent_at for m in prior if m.sent_at), default=None)
        if last and utcnow() - last < timedelta(days=s.outreach_per_prospect_cooldown_days):
            reasons.append(f"Cooldown: last message sent {last:%Y-%m-%d}; wait {s.outreach_per_prospect_cooldown_days} days")
        dup = db.scalar(select(OutreachMessage.id).where(
            OutreachMessage.id != msg.id, OutreachMessage.to_email == msg.to_email,
            OutreachMessage.subject == msg.subject, OutreachMessage.status.in_(SENT_STATES + ("approved", "queued"))))
        if dup:
            reasons.append(f"Duplicate of message #{dup} to the same address")
    if at_send and not msg.simulated and sent_today(db) >= s.outreach_daily_cap:
        reasons.append(f"Daily send cap reached ({s.outreach_daily_cap})")
    return reasons


def bounce_or_complaint(db: Session, email: str, reason: str, source: str) -> None:
    suppress(db, email, reason, source=source)


def calling_window_ok(now_local) -> tuple[bool, str]:
    """Texas Bus. & Com. Code §301.051 hours for telephone solicitation (applied to all business calls)."""
    s = get_settings()
    start = s.calling_sunday_start if now_local.weekday() == 6 else s.calling_hours_start
    if not (start <= now_local.hour < s.calling_hours_end):
        return False, f"Outside permitted calling hours ({start}:00–{s.calling_hours_end}:00 local)"
    return True, ""


