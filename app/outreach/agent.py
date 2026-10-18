"""The persistent outreach agent.

The agent is not a long-running conversation: it is a sequence of bounded
"ticks" run by the durable worker (job ``outreach.agent_tick``). Each tick
records an AgentRun with an explicit state and a checkpoint per phase:

  inbox -> recover -> followups -> drafting -> sending -> replenish

Every outbound action passes app/outreach/policy.py checks immediately before
it is executed, and every action/outcome/error/cost is written to the audit log.
"""
from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.audit import audit
from app.core.jobs import cancel_jobs, enqueue, get_flag, set_flag
from app.core.time import new_id, utcnow
from app.models import (
    AgentRun, Campaign, CompanyFact, CostEntry, Enrollment, Escalation, Meeting, OutreachMessage, PlatformIntegration, Prospect,
    ProspectContact,
)
from app.outreach import classifier, drafting, policy
from app.outreach.providers import build as build_provider, is_live
from app.outreach.providers.base import OutboundEmail, ProviderError

MAX_INBOUND = 50
MAX_DRAFTS = 10
MAX_SENDS = 20


def _audit(db, action, entity_type, entity_id=None, cost=None, **details):
    audit(db, action=action, entity_type=entity_type, entity_id=entity_id, actor_type="agent", actor_id="outreach-agent",
          details=details, cost_usd=cost)


def escalate(db: Session, *, kind: str, summary: str, prospect_id: str | None = None, message_id: str | None = None) -> Escalation:
    e = Escalation(kind=kind, summary=summary[:2000], prospect_id=prospect_id, message_id=message_id)
    db.add(e)
    db.flush()
    _audit(db, "agent.escalated", "escalation", e.id, kind=kind, prospect_id=prospect_id)
    return e


# ------------------------------------------------------------------ controls

def emergency_stop(db: Session, user_id: str, reason: str) -> dict:
    set_flag(db, policy.EMERGENCY_STOP, True, user_id)
    cancelled = cancel_jobs(db, type_prefix="outreach.")
    held = 0
    for m in db.scalars(select(OutreachMessage).where(OutreachMessage.direction == "outbound",
                                                      OutreachMessage.status.in_(("approved", "awaiting_approval", "draft")))).all():
        m.status, m.error = "blocked", f"Emergency stop: {reason}"
        held += 1
    audit(db, action="agent.emergency_stop", entity_type="system", actor_type="user", actor_id=user_id,
          details={"reason": reason, "cancelled_jobs": cancelled, "held_messages": held})
    db.commit()
    return {"cancelled_jobs": cancelled, "held_messages": held}


def clear_emergency_stop(db: Session, user_id: str) -> None:
    set_flag(db, policy.EMERGENCY_STOP, False, user_id)
    audit(db, action="agent.emergency_stop_cleared", entity_type="system", actor_type="user", actor_id=user_id,
          details={"note": "Messages blocked by the stop stay blocked; re-approve them individually."})


def set_global_pause(db: Session, paused: bool, user_id: str) -> None:
    set_flag(db, policy.GLOBAL_PAUSE, paused, user_id)
    audit(db, action="agent.paused" if paused else "agent.resumed", entity_type="system", actor_type="user", actor_id=user_id)


def agent_state(db: Session) -> str:
    if get_flag(db, policy.EMERGENCY_STOP, False):
        return "emergency_stopped"
    if get_flag(db, policy.GLOBAL_PAUSE, False):
        return "paused"
    last = db.scalar(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(1))
    if not last:
        return "never_run"
    if last.state == "running" and last.started_at < utcnow() - timedelta(minutes=15):
        return "stalled"
    return {"error": "degraded"}.get(last.state, "active" if db.scalar(select(func.count()).select_from(Campaign)
                                                                       .where(Campaign.status == "active")) else "idle")


# ------------------------------------------------------------------ helpers

def _email_integration(db: Session, campaign: Campaign | None) -> PlatformIntegration | None:
    if campaign and campaign.email_integration_id:
        return db.get(PlatformIntegration, campaign.email_integration_id)
    return db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == "email").order_by(PlatformIntegration.created_at))


def _calendar_integration(db: Session, campaign: Campaign | None) -> PlatformIntegration | None:
    if campaign and campaign.calendar_integration_id:
        return db.get(PlatformIntegration, campaign.calendar_integration_id)
    return db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == "calendar").order_by(PlatformIntegration.created_at))


def _record_cost(db: Session, category: str, provider: str, amount: float, measured: bool, campaign_id: str | None, ref: str,
                 units: float = 1, unit_label: str = "") -> None:
    db.add(CostEntry(category=category, provider=provider, amount_usd=amount, measured=measured, campaign_id=campaign_id,
                     ref=ref, units=units, unit_label=unit_label))


def stop_enrollments(db: Session, prospect_id: str, reason: str, state: str = "stopped") -> int:
    n = 0
    for e in db.scalars(select(Enrollment).where(Enrollment.prospect_id == prospect_id,
                                                 Enrollment.state.not_in(("stopped", "completed")))).all():
        e.state, e.stop_reason, e.next_action_at = state, reason, None
        n += 1
    for m in db.scalars(select(OutreachMessage).where(OutreachMessage.prospect_id == prospect_id, OutreachMessage.kind == "sequence",
                                                      OutreachMessage.status.in_(("draft", "awaiting_approval", "approved")))).all():
        m.status, m.error = "blocked", f"Sequence stopped: {reason}"
    return n


# ------------------------------------------------------------------ enrollment & drafting

def enroll(db: Session, campaign: Campaign, prospect: Prospect) -> Enrollment | None:
    if prospect.status != "qualified":
        return None
    if db.scalar(select(Enrollment).where(Enrollment.campaign_id == campaign.id, Enrollment.prospect_id == prospect.id)):
        return None
    if db.scalar(select(Enrollment).where(Enrollment.prospect_id == prospect.id, Enrollment.state.not_in(("stopped", "completed")))):
        return None  # never run two sequences at one prospect
    contacts = db.scalars(select(ProspectContact).where(ProspectContact.prospect_id == prospect.id,
                                                        ProspectContact.email.is_not(None),
                                                        ProspectContact.evidence_url.is_not(None),
                                                        ProspectContact.email_status.not_in(("bounced", "invalid")))).all()
    contact = next((c for c in contacts if not policy.is_suppressed(db, c.email)), None)
    if not contact:
        return None
    e = Enrollment(campaign_id=campaign.id, prospect_id=prospect.id, contact_id=contact.id, state="needs_draft",
                   next_action_at=utcnow())
    db.add(e)
    prospect.status = "enrolled"
    db.flush()
    _audit(db, "agent.enrolled", "enrollment", e.id, campaign_id=campaign.id, prospect_id=prospect.id)
    return e


def _step_def(campaign: Campaign, step: int) -> dict:
    seq = campaign.sequence or [{"step": 1, "delay_days": 0, "goal": "intro"}, {"step": 2, "delay_days": 4, "goal": "follow_up"},
                                {"step": 3, "delay_days": 7, "goal": "breakup"}]
    return next((s for s in seq if s.get("step") == step), {"step": step, "delay_days": 4, "goal": "follow_up"})


def draft_next(db: Session, e: Enrollment) -> OutreachMessage | None:
    campaign = db.get(Campaign, e.campaign_id)
    prospect = db.get(Prospect, e.prospect_id)
    contact = db.get(ProspectContact, e.contact_id)
    step = e.current_step + 1
    if step > 1 + campaign.max_followups:
        e.state, e.stop_reason, e.next_action_at = "completed", "sequence finished without reply", None
        if prospect.status in ("enrolled", "contacted"):
            prospect.status = "contacted"
        return None
    existing = db.scalar(select(OutreachMessage).where(OutreachMessage.idempotency_key == f"enr:{e.id}:step:{step}"))
    if existing:  # already drafted (e.g. state was reset); never create a second message for a step
        e.state = {"awaiting_approval": "awaiting_approval", "approved": "scheduled", "sent": "waiting"}.get(existing.status, e.state)
        _audit(db, "agent.duplicate_draft_prevented", "outreach_message", existing.id, step=step)
        return None
    d = drafting.draft(db, campaign=campaign, prospect=prospect, contact=contact, step=step, goal=_step_def(campaign, step).get("goal", "follow_up"))
    integration = _email_integration(db, campaign)
    mid = new_id("msg")
    body = d["body"] + drafting.footer(campaign, policy.unsubscribe_url(mid))
    needs_approval = campaign.require_approval_first_touch if step == 1 else campaign.require_approval_followups
    if integration and is_live(integration) and not campaign.live_authorized:
        needs_approval = True
    prev = db.scalar(select(OutreachMessage).where(OutreachMessage.enrollment_id == e.id, OutreachMessage.status == "sent")
                     .order_by(OutreachMessage.sent_at.desc()).limit(1))
    msg = OutreachMessage(id=mid, enrollment_id=e.id, prospect_id=prospect.id, contact_id=contact.id, direction="outbound",
                          channel=campaign.channel, step=step, kind="sequence", subject=d["subject"], body=body,
                          from_address=campaign.sender_email, to_address=contact.email,
                          status="awaiting_approval" if needs_approval else "approved", approval_required=needs_approval,
                          integration_id=integration.id if integration else None, thread_id=prev.thread_id if prev else e.thread_id,
                          idempotency_key=f"enr:{e.id}:step:{step}", send_after=e.next_action_at or utcnow(),
                          evidence_refs=d["evidence_refs"], generator=d["meta"].get("generator"))
    db.add(msg)
    e.state = "awaiting_approval" if needs_approval else "scheduled"
    if d["meta"].get("cost_usd"):
        _record_cost(db, "ai", "anthropic", d["meta"]["cost_usd"], True, campaign.id, msg.id,
                     units=d["meta"].get("input_tokens", 0) + d["meta"].get("output_tokens", 0), unit_label="tokens")
    db.flush()
    _audit(db, "agent.drafted", "outreach_message", msg.id, step=step, approval_required=needs_approval,
           generator=msg.generator, cost=d["meta"].get("cost_usd"))
    return msg


def approve_message(db: Session, message_id: str, user_id: str, edits: dict | None = None) -> OutreachMessage:
    m = db.get(OutreachMessage, message_id)
    if not m or m.direction != "outbound":
        raise ValueError("Message not found")
    if m.status not in ("awaiting_approval", "blocked", "draft"):
        raise ValueError(f"Message is {m.status}")
    if edits:
        if "subject" in edits:
            m.subject = edits["subject"]
        if "body" in edits:
            # The compliance footer (postal address + unsubscribe link) is always kept.
            cut = m.body.rfind("\n\n--\n")
            footer_text = m.body[cut:] if cut >= 0 else ""
            new = edits["body"]
            ncut = new.rfind("\n\n--\n")
            m.body = (new[:ncut] if ncut >= 0 else new).rstrip() + footer_text
    m.status, m.approved_by, m.approved_at, m.error = "approved", user_id, utcnow(), None
    if m.enrollment_id:
        e = db.get(Enrollment, m.enrollment_id)
        if e.state == "awaiting_approval":
            e.state = "scheduled"
    audit(db, action="outreach.message_approved", entity_type="outreach_message", entity_id=m.id, actor_type="user", actor_id=user_id,
          details={"edited": bool(edits)})
    db.flush()
    return m


def reject_message(db: Session, message_id: str, user_id: str, reason: str) -> OutreachMessage:
    m = db.get(OutreachMessage, message_id)
    m.status, m.error = "rejected", reason
    if m.enrollment_id:
        e = db.get(Enrollment, m.enrollment_id)
        if m.kind == "sequence":
            e.state, e.stop_reason = "stopped", f"draft rejected: {reason}"
    audit(db, action="outreach.message_rejected", entity_type="outreach_message", entity_id=m.id, actor_type="user",
          actor_id=user_id, details={"reason": reason})
    db.flush()
    return m


# ------------------------------------------------------------------ sending

def send_message(db: Session, m: OutreachMessage, now: datetime | None = None) -> str:
    """Policy-check then send one approved message. Returns the resulting status."""
    now = now or utcnow()
    e = db.get(Enrollment, m.enrollment_id) if m.enrollment_id else None
    campaign = db.get(Campaign, e.campaign_id) if e else None
    prospect = db.get(Prospect, m.prospect_id)
    contact = db.get(ProspectContact, m.contact_id) if m.contact_id else None
    integration = db.get(PlatformIntegration, m.integration_id) if m.integration_id else _email_integration(db, campaign)
    if not integration:
        m.error = "No email integration configured"
        return m.status
    checks = policy.evaluate_send(db, m, campaign=campaign, enrollment=e, prospect=prospect, contact=contact,
                                  integration=integration, now=now)
    m.policy_checks = checks
    bad = policy.failed(checks)
    if bad:
        if set(bad) <= policy.SOFT:
            if "operating_schedule" in bad and campaign:
                m.send_after = policy.next_window_start(campaign, now)
            else:
                m.send_after = now + timedelta(minutes=30)
            m.error = "Deferred: " + ", ".join(bad)
            _audit(db, "agent.send_deferred", "outreach_message", m.id, failed=bad)
            return m.status
        m.status, m.error = "blocked", "Blocked by policy: " + ", ".join(bad)
        _audit(db, "agent.send_blocked", "outreach_message", m.id, failed=bad)
        if e and m.kind == "sequence" and any(b in bad for b in ("not_suppressed", "prospect_contactable", "enrollment_active")):
            e.state, e.stop_reason = "stopped", m.error
        return m.status
    # Duplicate guard: mark as sending and commit BEFORE calling the provider.
    m.status, m.attempts, m.sent_at = "sending", m.attempts + 1, now
    db.commit()
    provider = build_provider(db, integration)
    try:
        res = provider.send(OutboundEmail(to=m.to_address, from_address=m.from_address or "", from_name=campaign.sender_name if campaign else "",
                                          subject=m.subject, body=m.body, idempotency_key=m.idempotency_key, thread_id=m.thread_id,
                                          unsubscribe_url=policy.unsubscribe_url(m.id)))
    except ProviderError as exc:
        m.error = str(exc)
        if exc.retryable and m.attempts < 3:
            m.status, m.sent_at, m.send_after = "approved", None, now + timedelta(minutes=5 * 2 ** m.attempts)
            _audit(db, "agent.send_retry", "outreach_message", m.id, error=str(exc), attempt=m.attempts)
        else:
            m.status = "failed"
            escalate(db, kind="send_failed", summary=f"Sending to {m.to_address} failed: {exc}", prospect_id=m.prospect_id, message_id=m.id)
        integration.health_status, integration.health_detail, integration.health_checked_at = "degraded", str(exc)[:300], now
        return m.status
    m.status, m.provider, m.provider_message_id, m.thread_id = "sent", provider.name, res.provider_message_id, res.thread_id or m.thread_id
    m.error = None
    _record_cost(db, "email", provider.name, 0.0, False, campaign.id if campaign else None, m.id, unit_label="email")
    if e:
        e.thread_id = e.thread_id or m.thread_id
        if m.kind == "sequence":
            e.current_step = m.step or e.current_step
            nxt = _step_def(campaign, e.current_step + 1)
            e.state, e.next_action_at = "waiting", now + timedelta(days=int(nxt.get("delay_days", 4)))
    if prospect.status == "enrolled":
        prospect.status = "contacted"
    _audit(db, "agent.sent", "outreach_message", m.id, to=m.to_address, provider=provider.name, live=provider.live, step=m.step,
           kind=m.kind)
    return m.status


def recover_interrupted_sends(db: Session, now: datetime | None = None) -> int:
    """A message left in 'sending' means the process died mid-send. The outcome is
    unknown, so never resend automatically: mark failed and escalate."""
    now = now or utcnow()
    n = 0
    for m in db.scalars(select(OutreachMessage).where(OutreachMessage.status == "sending",
                                                      OutreachMessage.sent_at < now - timedelta(minutes=10))).all():
        m.status, m.error = "failed", "Send interrupted; outcome unknown. Check the mailbox before resending."
        escalate(db, kind="send_outcome_unknown", summary=m.error + f" (to {m.to_address})", prospect_id=m.prospect_id, message_id=m.id)
        n += 1
    return n


# ------------------------------------------------------------------ inbound

def _find_context(db: Session, inbound) -> tuple[OutreachMessage | None, ProspectContact | None]:
    out = None
    if inbound.thread_id:
        out = db.scalar(select(OutreachMessage).where(OutreachMessage.thread_id == inbound.thread_id,
                                                      OutreachMessage.direction == "outbound").order_by(OutreachMessage.created_at.desc()).limit(1))
    contact = db.scalar(select(ProspectContact).where(ProspectContact.email == inbound.from_address.lower()))
    if not out and contact:
        out = db.scalar(select(OutreachMessage).where(OutreachMessage.contact_id == contact.id, OutreachMessage.direction == "outbound",
                                                      OutreachMessage.status == "sent").order_by(OutreachMessage.sent_at.desc()).limit(1))
    if out and not contact and out.contact_id:
        contact = db.get(ProspectContact, out.contact_id)
    return out, contact


def answer_for(db: Session, text: str) -> CompanyFact | None:
    low = text.lower()
    best, best_hits = None, 0
    for f in db.scalars(select(CompanyFact).where(CompanyFact.active.is_(True), CompanyFact.approved_by.is_not(None))).all():
        hits = sum(1 for k in f.keywords if k.lower() in low)
        if hits > best_hits:
            best, best_hits = f, hits
    return best


def _reply(db: Session, *, inbound_msg: OutreachMessage, e: Enrollment | None, campaign: Campaign | None, kind: str, body: str,
           auto: bool) -> OutreachMessage:
    integration = _email_integration(db, campaign)
    mid = new_id("msg")
    subject = inbound_msg.subject if inbound_msg.subject.lower().startswith("re:") else f"Re: {inbound_msg.subject}"
    full = body + (drafting.footer(campaign, policy.unsubscribe_url(mid)) if campaign else "")
    approval = not auto or (integration is not None and is_live(integration) and not (campaign and campaign.live_authorized))
    m = OutreachMessage(id=mid, enrollment_id=e.id if e else None, prospect_id=inbound_msg.prospect_id, contact_id=inbound_msg.contact_id,
                        direction="outbound", channel="email", kind=kind, subject=subject, body=full,
                        from_address=campaign.sender_email if campaign else None, to_address=inbound_msg.from_address,
                        status="awaiting_approval" if approval else "approved", approval_required=approval,
                        integration_id=integration.id if integration else None, thread_id=inbound_msg.thread_id,
                        idempotency_key=f"reply:{inbound_msg.id}:{kind}", send_after=None, generator="template")
    db.add(m)
    db.flush()
    _audit(db, "agent.reply_drafted", "outreach_message", m.id, kind=kind, approval_required=approval)
    return m


def offer_meeting(db: Session, *, inbound_msg: OutreachMessage, e: Enrollment, campaign: Campaign, now: datetime) -> Meeting | None:
    cal_int = _calendar_integration(db, campaign)
    if not cal_int:
        escalate(db, kind="meeting_request", summary="Prospect wants a meeting but no calendar integration is configured",
                 prospect_id=inbound_msg.prospect_id, message_id=inbound_msg.id)
        return None
    cal = build_provider(db, cal_int)
    sch = campaign.operating_schedule or {}
    tz = ZoneInfo(sch.get("timezone", "America/Chicago"))
    start_h, end_h, days = sch.get("start_hour", 9), sch.get("end_hour", 17), sch.get("days", [0, 1, 2, 3, 4])
    horizon_start, horizon_end = now + timedelta(hours=20), now + timedelta(days=8)
    try:
        busy = cal.busy(horizon_start, horizon_end)
    except ProviderError as exc:
        escalate(db, kind="calendar_unavailable", summary=f"Could not read calendar: {exc}", prospect_id=inbound_msg.prospect_id,
                 message_id=inbound_msg.id)
        return None
    slots = []
    day = horizon_start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    while len(slots) < 3 and day < horizon_end:
        if day.weekday() in days and start_h + 1 <= day.hour < end_h - 1 and day.hour in (10, 14):
            s, en = day.astimezone(ZoneInfo("UTC")), (day + timedelta(minutes=30)).astimezone(ZoneInfo("UTC"))
            if s > horizon_start and not any(b.start < en and b.end > s for b in busy):
                slots.append({"start": s.isoformat(), "end": en.isoformat(), "label": day.strftime("%A %B %-d at %-I:%M %p")})
        day += timedelta(hours=1)
    if not slots:
        escalate(db, kind="meeting_request", summary="No free slots in the next week", prospect_id=inbound_msg.prospect_id,
                 message_id=inbound_msg.id)
        return None
    mtg = Meeting(prospect_id=inbound_msg.prospect_id, enrollment_id=e.id, status="offered", offered_slots=slots,
                  provider=cal.name, attendee_email=inbound_msg.from_address)
    db.add(mtg)
    db.flush()
    tzname = sch.get("timezone", "America/Chicago").split("/")[-1].replace("_", " ")
    lines = "\n".join(f"  Option {i + 1}: {s['label']} ({tzname} time)" for i, s in enumerate(slots))
    body = (f"Thanks for getting back to me. Here are a few times for a 30-minute video call:\n\n{lines}\n\n"
            "Reply with the option number that works best, or suggest another time.")
    _reply(db, inbound_msg=inbound_msg, e=e, campaign=campaign, kind="meeting_offer", body=body, auto=campaign.auto_reply_routine)
    _audit(db, "agent.meeting_offered", "meeting", mtg.id, slots=len(slots))
    return mtg


def book_meeting(db: Session, mtg: Meeting, slot_index: int, *, booked_by: str, now: datetime | None = None) -> Meeting:
    now = now or utcnow()
    slot = mtg.offered_slots[slot_index]
    start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
    e = db.get(Enrollment, mtg.enrollment_id) if mtg.enrollment_id else None
    campaign = db.get(Campaign, e.campaign_id) if e else None
    cal_int = _calendar_integration(db, campaign)
    if get_flag(db, policy.EMERGENCY_STOP, False):
        raise ProviderError("Emergency stop is active")
    if not cal_int:
        raise ProviderError("No calendar integration configured")
    if is_live(cal_int) and not (cal_int.live_authorized and settings.outreach_live_allowed):
        raise ProviderError("Calendar integration is not authorised for live use")
    cal = build_provider(db, cal_int)
    if any(b.start < end and b.end > start for b in cal.busy(start - timedelta(minutes=1), end + timedelta(minutes=1))):
        raise ProviderError("That slot is no longer free")
    prospect = db.get(Prospect, mtg.prospect_id)
    key = f"meeting:{mtg.id}:{slot['start']}"
    event_id, link = cal.create_event(start=start, end=end, attendee_email=mtg.attendee_email,
                                      summary=f"Intro call - {prospect.business_name}",
                                      description="Introductory call about bilingual vehicle listings and dealership marketing.",
                                      idempotency_key=key)
    mtg.status, mtg.start_at, mtg.end_at, mtg.calendar_event_id, mtg.video_link = "booked", start, end, event_id, link
    mtg.booked_by, mtg.idempotency_key = booked_by, key
    prospect.status = "meeting_booked"
    if e:
        e.state, e.stop_reason, e.next_action_at = "meeting_booked", "meeting booked", None
    stop_enrollments(db, prospect.id, "meeting booked", state="meeting_booked")
    _audit(db, "agent.meeting_booked", "meeting", mtg.id, start=slot["start"], provider=cal.name, live=cal.live, booked_by=booked_by)
    return mtg


def process_inbound(db: Session, inbound, now: datetime | None = None) -> OutreachMessage | None:
    now = now or utcnow()
    if db.scalar(select(OutreachMessage).where(OutreachMessage.provider_message_id == inbound.provider_message_id,
                                               OutreachMessage.direction == "inbound")):
        return None  # already processed (idempotent)
    out, contact = _find_context(db, inbound)
    e = db.get(Enrollment, out.enrollment_id) if out and out.enrollment_id else None
    campaign = db.get(Campaign, e.campaign_id) if e else None
    offered = db.scalar(select(Meeting).where(Meeting.enrollment_id == e.id, Meeting.status == "offered")
                        .order_by(Meeting.created_at.desc()).limit(1)) if e else None
    c = classifier.classify(from_address=inbound.from_address, subject=inbound.subject, body=inbound.body,
                            offered_slots=offered.offered_slots if offered else None)
    msg = OutreachMessage(enrollment_id=e.id if e else None, prospect_id=(out.prospect_id if out else contact.prospect_id if contact else None),
                          contact_id=contact.id if contact else None, direction="inbound", channel="email", kind="reply",
                          subject=inbound.subject[:300], body=inbound.body[:20000], from_address=inbound.from_address,
                          to_address=inbound.to_address, status="received", approval_required=False,
                          provider_message_id=inbound.provider_message_id, thread_id=inbound.thread_id,
                          idempotency_key=f"in:{inbound.provider_message_id}", received_at=inbound.received_at,
                          classification=c.category, classification_confidence=c.confidence, classification_method=c.method)
    db.add(msg)
    db.flush()
    _audit(db, "agent.reply_received", "outreach_message", msg.id, classification=c.category, confidence=c.confidence,
           method=c.method, matched=bool(msg.prospect_id))
    prospect = db.get(Prospect, msg.prospect_id) if msg.prospect_id else None

    if c.category == "opt_out" or c.category == "complaint":
        # Suppress first, regardless of whether we can match the sender.
        policy.suppress(db, "email", inbound.from_address, "opt_out" if c.category == "opt_out" else "complaint", f"reply:{msg.id}")
        if prospect:
            prospect.status = "do_not_contact"
            stop_enrollments(db, prospect.id, c.category)
        if c.category == "complaint":
            escalate(db, kind="complaint", summary=f"Complaint from {inbound.from_address}: {inbound.body[:300]}",
                     prospect_id=msg.prospect_id, message_id=msg.id)
        msg.status = "processed"
        return msg
    if not prospect:
        escalate(db, kind="unmatched_reply", summary=f"Reply from {inbound.from_address} could not be matched to a prospect",
                 message_id=msg.id)
        msg.status = "processed"
        return msg
    if c.category == "bounce":
        if out and out.to_address:
            policy.suppress(db, "email", out.to_address, "bounce", f"bounce:{msg.id}")
            ct = db.get(ProspectContact, out.contact_id) if out.contact_id else None
            if ct:
                ct.email_status = "bounced"
        stop_enrollments(db, prospect.id, "email bounced")
        prospect.status = "researching"
    elif c.category == "out_of_office":
        if e and e.state == "waiting":
            e.next_action_at = now + timedelta(days=7)
    elif c.category == "not_interested":
        stop_enrollments(db, prospect.id, "not interested")
        prospect.status = "lost"
        policy.suppress(db, "email", inbound.from_address, "not_interested", f"reply:{msg.id}", expires_at=now + timedelta(days=365))
    elif c.category == "referral":
        stop_enrollments(db, prospect.id, "referred elsewhere")
        prospect.status = "replied"
        escalate(db, kind="referral", summary=f"Prospect referred us to someone else; verify the new contact manually: {inbound.body[:400]}",
                 prospect_id=prospect.id, message_id=msg.id)
    elif c.category == "sensitive":
        stop_enrollments(db, prospect.id, "replied (sensitive)", state="replied")
        prospect.status = "replied"
        escalate(db, kind="sensitive", summary=f"Sensitive topic needs a human: {inbound.body[:400]}", prospect_id=prospect.id, message_id=msg.id)
    elif c.category == "meeting_slot_selection" and offered and e:
        prospect.status = "replied"
        if campaign and campaign.auto_book_meetings:
            try:
                book_meeting(db, offered, c.slot_index, booked_by="agent", now=now)
                body = (f"Confirmed: {offered.offered_slots[c.slot_index]['label']}. A calendar invitation"
                        f"{' with the video link' if offered.video_link else ''} is on its way. Talk soon.")
                _reply(db, inbound_msg=msg, e=e, campaign=campaign, kind="meeting_confirmation", body=body, auto=True)
            except ProviderError as exc:
                escalate(db, kind="meeting_booking_failed", summary=f"Could not book the chosen slot: {exc}", prospect_id=prospect.id,
                         message_id=msg.id)
        else:
            escalate(db, kind="meeting_approval", summary=f"Prospect chose option {c.slot_index + 1}; approve to book "
                     f"({offered.offered_slots[c.slot_index]['label']})", prospect_id=prospect.id, message_id=msg.id)
    elif c.category in ("meeting_request", "positive_interest") and e and campaign:
        stop_enrollments(db, prospect.id, "prospect replied", state="replied")
        prospect.status = "replied"
        offer_meeting(db, inbound_msg=msg, e=e, campaign=campaign, now=now)
    elif c.category == "question" and e and campaign:
        stop_enrollments(db, prospect.id, "prospect replied", state="replied")
        prospect.status = "replied"
        fact = answer_for(db, inbound.body)
        if fact:
            _reply(db, inbound_msg=msg, e=e, campaign=campaign, kind="reply",
                   body=f"Thanks for the question.\n\n{fact.answer}\n\nHappy to go over it on a short call if useful.",
                   auto=campaign.auto_reply_routine)
        else:
            escalate(db, kind="question", summary=f"Question without an approved answer: {inbound.body[:400]}", prospect_id=prospect.id,
                     message_id=msg.id)
    else:
        if e:
            stop_enrollments(db, prospect.id, "prospect replied", state="replied")
        prospect.status = "replied"
        escalate(db, kind="uncertain_reply", summary=f"Could not confidently classify reply ({c.category}, {c.confidence:.2f}): "
                 f"{inbound.body[:300]}", prospect_id=prospect.id, message_id=msg.id)
    msg.status = "processed"
    return msg


# ------------------------------------------------------------------ tick

def tick(db: Session, *, job_id: str | None = None, now: datetime | None = None) -> AgentRun:
    now = now or utcnow()
    run = AgentRun(state="running", job_id=job_id, started_at=now, stats={})
    db.add(run)
    db.commit()
    stats: dict[str, Any] = {}
    try:
        if get_flag(db, policy.EMERGENCY_STOP, False):
            run.state = "stopped"
            return run
        if get_flag(db, policy.GLOBAL_PAUSE, False):
            run.state = "skipped_paused"
            return run
        phases = [("inbox", _phase_inbox), ("recover", lambda d, n: {"interrupted": recover_interrupted_sends(d, n)}),
                  ("followups", _phase_followups), ("drafting", _phase_drafting), ("sending", _phase_sending),
                  ("replenish", _phase_replenish)]
        for name, fn in phases:
            if get_flag(db, policy.EMERGENCY_STOP, False):
                run.state = "stopped"
                stats["stopped_at"] = name
                return run
            try:
                stats[name] = fn(db, now)
            except Exception as exc:  # noqa: BLE001 - one phase failing must not stop the others
                db.rollback()
                stats[name] = {"error": f"{type(exc).__name__}: {exc}"}
                _audit(db, "agent.phase_error", "agent_run", run.id, phase=name, error=str(exc)[:500],
                       trace=traceback.format_exc(limit=3)[-800:])
            run.phase, run.stats = name, dict(stats)
            db.commit()
        run.state = "error" if any(isinstance(v, dict) and "error" in v for v in stats.values()) else "completed"
        return run
    finally:
        run.stats, run.finished_at = dict(stats), utcnow()
        db.commit()


def _phase_inbox(db: Session, now: datetime) -> dict:
    processed = 0
    for integ in db.scalars(select(PlatformIntegration).where(PlatformIntegration.kind == "email")).all():
        if is_live(integ) and not integ.live_authorized:
            continue
        provider = build_provider(db, integ)
        since = (integ.config or {}).get("inbox_cursor")
        try:
            items = provider.fetch_inbound(datetime.fromisoformat(since) if since else None, MAX_INBOUND)
        except ProviderError as exc:
            integ.health_status, integ.health_detail, integ.health_checked_at = "down", str(exc)[:300], now
            _audit(db, "agent.inbox_error", "platform_integration", integ.id, error=str(exc))
            continue
        for item in items:
            if process_inbound(db, item, now):
                processed += 1
            db.commit()
        if items:
            integ.config = {**(integ.config or {}), "inbox_cursor": max(i.received_at for i in items).isoformat()}
    return {"processed": processed}


def _phase_followups(db: Session, now: datetime) -> dict:
    due = db.scalars(select(Enrollment).where(Enrollment.state == "waiting", Enrollment.next_action_at <= now).limit(MAX_DRAFTS)).all()
    n = 0
    for e in due:
        campaign = db.get(Campaign, e.campaign_id)
        if campaign.status != "active":
            continue
        e.state = "needs_draft"
        n += 1
    return {"due": n}


def _phase_drafting(db: Session, now: datetime) -> dict:
    drafted = 0
    for e in db.scalars(select(Enrollment).where(Enrollment.state == "needs_draft").limit(MAX_DRAFTS)).all():
        campaign = db.get(Campaign, e.campaign_id)
        if campaign.status != "active":
            continue
        if draft_next(db, e):
            drafted += 1
        db.commit()
    return {"drafted": drafted}


def _phase_sending(db: Session, now: datetime) -> dict:
    results: dict[str, int] = {}
    msgs = db.scalars(select(OutreachMessage).where(OutreachMessage.direction == "outbound", OutreachMessage.status == "approved",
                                                    (OutreachMessage.send_after.is_(None)) | (OutreachMessage.send_after <= now))
                      .order_by(OutreachMessage.created_at).limit(MAX_SENDS)).all()
    for m in msgs:
        status = send_message(db, m, now)
        results[status] = results.get(status, 0) + 1
        db.commit()
    return results


def _phase_replenish(db: Session, now: datetime) -> dict:
    enrolled = queued_discovery = queued_enrich = 0
    for campaign in db.scalars(select(Campaign).where(Campaign.status == "active")).all():
        tc = campaign.target_criteria or {}
        headroom = campaign.daily_send_limit - db.scalar(
            select(func.count()).select_from(Enrollment).where(Enrollment.campaign_id == campaign.id, Enrollment.created_at >= now - timedelta(hours=24)))
        for p in db.scalars(select(Prospect).where(Prospect.status == "qualified").order_by(Prospect.priority_score.desc())
                            .limit(max(0, headroom))).all():
            if enroll(db, campaign, p):
                enrolled += 1
        pool = db.scalar(select(func.count()).select_from(Prospect).where(Prospect.status.in_(("qualified", "new", "researching"))))
        if pool < int(tc.get("min_queue", 20)):
            disc = db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == "discovery").order_by(PlatformIntegration.created_at))
            if disc and (not is_live(disc) or disc.live_authorized):
                for q in tc.get("queries", ["used car dealer in Sugar Land, TX"]):
                    _, created = enqueue(db, "outreach.discover", payload={"integration_id": disc.id, "query": q, "campaign_id": campaign.id},
                                         queue="outreach", idempotency_key=f"discover:{disc.id}:{q}:{now.date().isoformat()}")
                    queued_discovery += int(created)
    for p in db.scalars(select(Prospect).where(Prospect.status == "new", Prospect.last_enriched_at.is_(None)).limit(10)).all():
        _, created = enqueue(db, "outreach.enrich", payload={"prospect_id": p.id}, queue="outreach", idempotency_key=f"enrich:{p.id}")
        queued_enrich += int(created)
    return {"enrolled": enrolled, "discovery_jobs": queued_discovery, "enrich_jobs": queued_enrich}
