"""Persistent autonomous outreach agent.

Architecture
- State machine (OutreachAgentState.state): STOPPED → RUNNING ⇄ PAUSED; any → EMERGENCY_STOPPED (requires reset).
- A recurring ``outreach.tick`` job (every 60s, run by the durable worker) inspects state and enqueues
  bounded tasks: discover, enrich/qualify, enroll, draft, send, poll inbox. Each task is its own job with
  an idempotency key scoped to the run generation, retries with backoff, and re-checks stop conditions.
- No long-running model conversation and no infinite loop: every invocation does a bounded amount of work
  and checkpoints progress in the database, so worker restarts resume cleanly.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.integrations import registry
from app.integrations.base import IntegrationError
from app.models import (
    Campaign,
    CostEntry,
    Enrollment,
    OutreachAgentState,
    OutreachFAQ,
    OutreachMessage,
    OutreachReply,
    Prospect,
)
from app.services import audit, controls, jobs, notifications
from app.services.outreach import classify as C
from app.services.outreach.booking import get_or_create_meeting
from app.services.outreach.compliance import add_suppression, check_message, seconds_until_window
from app.services.outreach.discovery import discover, qualify
from app.services.outreach.drafting import DEFAULT_STEPS, build_draft
from app.services.responder.language import strip_accents

TICK_SECONDS = 60
OUTREACH_JOB_TYPES = ["outreach.discover", "outreach.enrich", "outreach.draft", "outreach.send", "outreach.poll_inbox",
                      "outreach.handle_reply"]
MAX_PER_TICK = 25


def install_schedules(db: Session) -> None:
    jobs.ensure_recurring(db, "outreach.tick", "outreach.tick", TICK_SECONDS)


def get_state(db: Session) -> OutreachAgentState:
    st = db.scalar(select(OutreachAgentState).order_by(OutreachAgentState.id).limit(1))
    if st is None:
        st = OutreachAgentState()
        db.add(st)
        db.flush()
    return st


def _transition(db: Session, st: OutreachAgentState, new: str, *, user_id: int | None, reason: str | None) -> None:
    old = st.state
    st.state, st.state_reason, st.changed_by_id, st.changed_at = new, reason, user_id, utcnow()
    audit.record(db, "outreach.state_changed", actor_type="USER" if user_id else "AGENT", actor_user_id=user_id,
                 entity_type="outreach_agent", entity_id=st.id, old=old, new=new, reason=reason)


def start(db: Session, *, user_id: int, reason: str = "started by operator") -> OutreachAgentState:
    st = get_state(db)
    if st.state == "EMERGENCY_STOPPED" or controls.get_flag(db, controls.OUTREACH_EMERGENCY_STOP):
        raise ValueError("Emergency stop is active. Reset it explicitly before starting the agent.")
    st.run_generation += 1
    _transition(db, st, "RUNNING", user_id=user_id, reason=reason)
    return st


def pause(db: Session, *, user_id: int | None, reason: str) -> OutreachAgentState:
    st = get_state(db)
    if st.state == "RUNNING":
        _transition(db, st, "PAUSED", user_id=user_id, reason=reason)
    return st


def stop(db: Session, *, user_id: int, reason: str = "stopped by operator") -> OutreachAgentState:
    st = get_state(db)
    if st.state != "EMERGENCY_STOPPED":
        _transition(db, st, "STOPPED", user_id=user_id, reason=reason)
    jobs.cancel_jobs(db, OUTREACH_JOB_TYPES)
    return st


def emergency_stop(db: Session, *, user_id: int | None, reason: str) -> OutreachAgentState:
    """Immediate halt: flag + state + cancel queued outreach jobs. In-flight sends re-check before sending."""
    controls.set_flag(db, controls.OUTREACH_EMERGENCY_STOP, True, user_id=user_id, reason=reason)
    st = get_state(db)
    _transition(db, st, "EMERGENCY_STOPPED", user_id=user_id, reason=reason)
    cancelled = jobs.cancel_jobs(db, OUTREACH_JOB_TYPES)
    audit.record(db, "outreach.emergency_stop", actor_type="USER" if user_id else "SYSTEM", actor_user_id=user_id,
                 cancelled_jobs=cancelled, reason=reason)
    return st


def reset_emergency(db: Session, *, user_id: int, reason: str) -> OutreachAgentState:
    controls.set_flag(db, controls.OUTREACH_EMERGENCY_STOP, False, user_id=user_id, reason=reason)
    st = get_state(db)
    _transition(db, st, "STOPPED", user_id=user_id, reason=f"emergency stop reset: {reason}")
    return st


def _key(st: OutreachAgentState, *parts) -> str:
    return "outreach:" + ":".join(str(p) for p in (st.run_generation, *parts))


def _live_email(db: Session) -> bool:
    _, live = registry.outreach_email_sender(db)
    return live


@jobs.handler("outreach.tick")
def tick(db: Session, payload: dict, job) -> dict:
    st = get_state(db)
    st.last_tick_at = utcnow()
    if controls.get_flag(db, controls.OUTREACH_EMERGENCY_STOP) and st.state != "EMERGENCY_STOPPED":
        _transition(db, st, "EMERGENCY_STOPPED", user_id=None, reason="emergency stop flag set")
    if st.state != "RUNNING":
        return {"state": st.state}
    now = utcnow()
    slot = int(now.timestamp() // TICK_SECONDS)
    out: dict = {"state": "RUNNING"}

    # 1. Replenish the prospect queue when eligible prospects are scarce.
    pipeline = db.scalar(select(func.count(Prospect.id)).where(Prospect.status.in_(["NEW", "QUALIFIED"]))) or 0
    if pipeline < st.target_queue_size:
        jobs.enqueue(db, "outreach.discover", {"limit": min(st.max_discoveries_per_tick, st.target_queue_size - pipeline)},
                     idempotency_key=_key(st, "discover", slot // 15), max_attempts=st.max_task_retries)
        out["discover"] = True
    # 2. Research/qualify new prospects.
    new_ids = list(db.scalars(select(Prospect.id).where(Prospect.status == "NEW").limit(MAX_PER_TICK)))
    for pid in new_ids:
        jobs.enqueue(db, "outreach.enrich", {"prospect_id": pid}, idempotency_key=_key(st, "enrich", pid),
                     max_attempts=st.max_task_retries)
    # 3. Enroll qualified prospects (highest fit first) into the first active campaign.
    campaign = db.scalar(select(Campaign).where(Campaign.status == "ACTIVE").order_by(Campaign.id))
    enrolled = 0
    if campaign:
        for p in db.scalars(select(Prospect).where(Prospect.status == "QUALIFIED")
                            .order_by(Prospect.fit_score.desc(), Prospect.id).limit(MAX_PER_TICK)):
            if db.scalar(select(Enrollment.id).where(Enrollment.prospect_id == p.id, Enrollment.status == "ACTIVE")):
                continue
            if db.scalar(select(Enrollment.id).where(Enrollment.prospect_id == p.id, Enrollment.campaign_id == campaign.id)):
                continue
            db.add(Enrollment(campaign_id=campaign.id, prospect_id=p.id, next_action_at=now))
            p.status = "ENROLLED"
            enrolled += 1
    out["enrolled"] = enrolled
    db.flush()
    # 4. Draft due sequence steps.
    due = list(db.scalars(select(Enrollment).where(Enrollment.status == "ACTIVE", Enrollment.next_action_at <= now)
                          .limit(MAX_PER_TICK)))
    for enr in due:
        jobs.enqueue(db, "outreach.draft", {"enrollment_id": enr.id},
                     idempotency_key=_key(st, "draft", enr.id, enr.current_step), max_attempts=st.max_task_retries)
    # 5. Send approved messages (compliance re-checked inside the send job).
    approved = list(db.scalars(select(OutreachMessage.id).where(OutreachMessage.status == "APPROVED").limit(MAX_PER_TICK)))
    for mid in approved:
        jobs.enqueue(db, "outreach.send", {"message_id": mid}, idempotency_key=_key(st, "send", mid),
                     max_attempts=st.max_task_retries, priority=60)
    # 6. Monitor the inbox.
    jobs.enqueue(db, "outreach.poll_inbox", {}, idempotency_key=_key(st, "poll", slot // 2), max_attempts=2)
    st.last_checkpoint = {"at": now.isoformat(), "pipeline": pipeline, "new": len(new_ids), "due": len(due),
                          "approved_to_send": len(approved), "enrolled": enrolled, "generation": st.run_generation}
    return out


@jobs.handler("outreach.discover")
def discover_job(db: Session, payload: dict, job) -> dict:
    st = get_state(db)
    if st.state != "RUNNING":
        return {"skipped": st.state}
    source = registry.prospect_source(db)
    from app.services.outreach.compliance import spent_today_cents

    if spent_today_cents(db, st) + source.estimated_cost_cents_per_call > st.daily_budget_cents:
        return {"skipped": "daily budget reached"}
    try:
        return discover(db, source, st.target_cities or ["Sugar Land"], int(payload.get("limit", 10)),
                        run_ref=f"job:{job.id}")
    except IntegrationError as exc:
        if not exc.retryable:
            raise jobs.PermanentJobError(str(exc)) from exc
        raise


@jobs.handler("outreach.enrich")
def enrich_job(db: Session, payload: dict, job) -> dict:
    p = db.get(Prospect, jobs.payload_get(payload, "prospect_id"))
    if p is None or p.status != "NEW":
        return {"skipped": True}
    st = get_state(db)
    qualify(db, p, st.target_cities or [])
    return {"status": p.status}


@jobs.handler("outreach.draft")
def draft_job(db: Session, payload: dict, job) -> dict:
    enr = db.get(Enrollment, jobs.payload_get(payload, "enrollment_id"))
    if enr is None or enr.status != "ACTIVE":
        return {"skipped": True}
    msg = build_draft(db, enr)
    if msg is None:
        steps = db.get(Campaign, enr.campaign_id).steps or DEFAULT_STEPS
        if enr.current_step >= len(steps):
            enr.status, enr.stop_reason = "COMPLETED", "sequence finished"
        else:
            enr.status, enr.stop_reason = "STOPPED", "could not build draft from verified facts"
            prospect = db.get(Prospect, enr.prospect_id)
            prospect.status, prospect.status_reason = "NEEDS_REVIEW", "draft could not be built from available facts"
        return {"stopped": enr.stop_reason}
    enr.next_action_at = None  # wait until sent
    if msg.status == "PENDING_APPROVAL":
        notifications.notify_operator(db, kind="SYSTEM", subject="Outreach drafts awaiting approval",
                                      body="One or more outreach messages need your approval before sending.",
                                      link="/admin/outreach#approvals", dedupe_key=f"approvals:{utcnow():%Y-%m-%d}")
    audit.record(db, "outreach.draft_created", actor_type="AGENT", entity_type="outreach_message", entity_id=msg.id,
                 status=msg.status, approval_reasons=msg.approval_reasons)
    return {"message_id": msg.id, "status": msg.status}


def _on_send_dead(db: Session, payload: dict, job) -> None:
    msg = db.get(OutreachMessage, payload.get("message_id"))
    if msg and msg.status in ("APPROVED", "SENDING"):
        msg.status, msg.error = "FAILED", (job.last_error or "")[:1000]
        notifications.notify_operator(db, kind="SYSTEM", subject="Outreach send failed",
                                      body=f"Message #{msg.id} to {msg.to_address} failed: {msg.error[:300]}",
                                      link="/admin/outreach", dedupe_key=f"sendfail-o:{msg.id}")


@jobs.handler("outreach.send", on_dead=_on_send_dead)
def send_job(db: Session, payload: dict, job) -> dict:
    msg = db.get(OutreachMessage, jobs.payload_get(payload, "message_id"))
    if msg is None or msg.status not in ("APPROVED", "SENDING"):
        return {"skipped": msg.status if msg else "missing"}
    st = get_state(db)
    sender, live = registry.outreach_email_sender(db)
    result = check_message(db, msg, st, live=live)
    if result.stop:
        msg.status, msg.block_reasons = "BLOCKED", result.stop
        audit.record(db, "outreach.send_blocked", actor_type="AGENT", entity_type="outreach_message", entity_id=msg.id,
                     outcome="BLOCKED", reasons=result.stop)
        return {"blocked": result.stop}
    if result.wait:
        msg.block_reasons = result.wait
        if "outside operating schedule" in result.wait:
            raise jobs.RetryLater(seconds_until_window(st), "; ".join(result.wait))
        if any(w.startswith("agent is") for w in result.wait):
            raise jobs.RetryLater(300, "; ".join(result.wait))
        raise jobs.RetryLater(1800, "; ".join(result.wait))
    s = get_settings()
    msg.status = "SENDING"
    msg.attempts += 1
    domain = (s.outreach_sender_email or "sandbox.invalid").split("@")[-1]
    msg.rfc_message_id = msg.rfc_message_id or f"<outreach-{msg.id}-{secrets.token_hex(6)}@{domain}>"
    base = s.base_url.rstrip("/")
    headers = {"List-Unsubscribe": f"<{base}/u/{msg.unsubscribe_token}>",
               "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if msg.unsubscribe_token else {}
    res = sender.send(to=msg.to_address, subject=msg.subject, body=msg.body,
                      from_addr=s.outreach_sender_email or "outreach@sandbox.invalid", from_name=s.outreach_sender_name,
                      headers=headers, message_id=msg.rfc_message_id, in_reply_to=msg.in_reply_to)
    if not res.ok:
        msg.error = res.error
        msg.status = "APPROVED" if res.retryable else "FAILED"
        audit.record(db, "outreach.send_failed", actor_type="AGENT", entity_type="outreach_message", entity_id=msg.id,
                     outcome="FAILED", external=live, error=res.error)
        if res.retryable:
            raise RuntimeError(res.error)
        if "refused" in (res.error or ""):
            add_suppression(db, "EMAIL", msg.to_address, "BOUNCE", f"smtp refused message #{msg.id}")
        return {"failed": res.error}
    msg.status = res.status  # SENT or SIMULATED
    msg.sent_at = utcnow()
    msg.provider_message_id = res.provider_message_id
    msg.block_reasons = []
    db.add(CostEntry(scope="OUTREACH", campaign_id=msg.campaign_id, provider=getattr(sender, "name", "email"),
                     operation="email_send", amount_cents=0.0, is_estimate=True, simulated=not live,
                     reference=f"outreach_message:{msg.id}"))
    prospect = db.get(Prospect, msg.prospect_id)
    prospect.last_contacted_at = utcnow()
    if msg.enrollment_id:
        enr = db.get(Enrollment, msg.enrollment_id)
        campaign = db.get(Campaign, enr.campaign_id)
        steps = campaign.steps or DEFAULT_STEPS
        enr.current_step += 1
        if enr.current_step < len(steps):
            gap = steps[enr.current_step]["day_offset"] - steps[enr.current_step - 1]["day_offset"]
            enr.next_action_at = utcnow() + timedelta(days=max(1, gap))
        else:
            enr.status, enr.stop_reason, enr.next_action_at = "COMPLETED", "sequence finished", None
    audit.record(db, "outreach.sent" if live else "outreach.simulated", actor_type="AGENT", entity_type="outreach_message",
                 entity_id=msg.id, outcome="OK" if live else "SIMULATED", external=live, to=msg.to_address,
                 step=msg.step_index)
    return {"status": msg.status}


@jobs.handler("outreach.poll_inbox")
def poll_inbox_job(db: Session, payload: dict, job) -> dict:
    st = get_state(db)
    reader, live = registry.outreach_inbox(db)
    try:
        emails, cursor = reader.fetch_new(dict(st.inbox_cursor or {}))
    except IntegrationError as exc:
        audit.record(db, "outreach.inbox_error", actor_type="AGENT", outcome="FAILED", error=str(exc))
        if exc.retryable:
            raise
        raise jobs.PermanentJobError(str(exc)) from exc
    stored = 0
    for e in emails:
        if db.scalar(select(OutreachReply.id).where(OutreachReply.provider_message_id == e.message_id)):
            continue  # duplicate
        refs = [r for r in ([e.in_reply_to] + e.references) if r]
        original = db.scalar(select(OutreachMessage).where(OutreachMessage.rfc_message_id.in_(refs))) if refs else None
        prospect = db.get(Prospect, original.prospect_id) if original else db.scalar(
            select(Prospect).where(Prospect.email == e.from_addr.lower()))
        if original is None and prospect is None:
            continue  # unrelated mail: not stored
        reply = OutreachReply(prospect_id=prospect.id if prospect else None,
                              outreach_message_id=original.id if original else None, provider_message_id=e.message_id,
                              from_address=e.from_addr, subject=e.subject[:300], body=e.body[:20000],
                              received_at=e.received_at, is_simulated=e.simulated)
        db.add(reply)
        db.flush()
        jobs.enqueue(db, "outreach.handle_reply", {"reply_id": reply.id}, idempotency_key=f"outreach:reply:{reply.id}",
                     max_attempts=3, priority=30)
        stored += 1
    st.inbox_cursor = cursor
    return {"fetched": len(emails), "stored": stored, "live": live}


def _stop_enrollments(db: Session, prospect: Prospect, reason: str) -> None:
    for enr in db.scalars(select(Enrollment).where(Enrollment.prospect_id == prospect.id, Enrollment.status == "ACTIVE")):
        enr.status, enr.stop_reason, enr.next_action_at = "STOPPED", reason, None
    for m in db.scalars(select(OutreachMessage).where(OutreachMessage.prospect_id == prospect.id,
                                                      OutreachMessage.kind == "SEQUENCE",
                                                      OutreachMessage.status.in_(["DRAFT", "PENDING_APPROVAL", "APPROVED"]))):
        m.status = "CANCELLED"
        m.block_reasons = [reason]


def match_outreach_faq(db: Session, text: str) -> OutreachFAQ | None:
    norm = strip_accents(text.lower())
    best, hits_best = None, 0
    for f in db.scalars(select(OutreachFAQ).where(OutreachFAQ.active.is_(True), OutreachFAQ.approved_at.is_not(None))):
        hits = sum(1 for k in f.keywords or [] if strip_accents(k.lower()) in norm)
        if hits > hits_best:
            best, hits_best = f, hits
    return best


def _reply_message(db: Session, prospect: Prospect, reply: OutreachReply, body: str, *, auto: bool, reason: str) -> OutreachMessage:
    original = db.get(OutreachMessage, reply.outreach_message_id) if reply.outreach_message_id else None
    subj = reply.subject or (original.subject if original else "Re:")
    msg = OutreachMessage(prospect_id=prospect.id, kind="REPLY", channel="EMAIL", to_address=reply.from_address,
                          subject=subj if subj.lower().startswith("re:") else f"Re: {subj}", body=body,
                          status="APPROVED" if auto else "PENDING_APPROVAL",
                          approval_reasons=[] if auto else [reason], idempotency_key=f"reply:{reply.id}",
                          in_reply_to=reply.provider_message_id, campaign_id=original.campaign_id if original else None)
    db.add(msg)
    db.flush()
    return msg


@jobs.handler("outreach.handle_reply")
def handle_reply_job(db: Session, payload: dict, job) -> dict:
    reply = db.get(OutreachReply, jobs.payload_get(payload, "reply_id"))
    if reply is None or reply.handled:
        return {"skipped": True}
    st = get_state(db)
    c = C.classify_reply(reply.subject or "", reply.body, reply.from_address)
    reply.classification, reply.classification_confidence, reply.classification_signals = c.label, c.confidence, c.signals
    reply.requires_human = c.requires_human
    prospect = db.get(Prospect, reply.prospect_id) if reply.prospect_id else None
    action = "none"
    if prospect is None:
        reply.requires_human = True
        action = "unmatched reply; needs human"
    elif c.label == C.BOUNCE:
        add_suppression(db, "EMAIL", prospect.email, "BOUNCE", f"bounce reply #{reply.id}")
        _stop_enrollments(db, prospect, "bounced")
        prospect.status, prospect.status_reason = "NEEDS_REVIEW", "email bounced"
        action = "suppressed bounced address; stopped sequence"
    elif c.label == C.OOO:
        for enr in db.scalars(select(Enrollment).where(Enrollment.prospect_id == prospect.id, Enrollment.status == "ACTIVE")):
            if enr.next_action_at:
                enr.next_action_at = max(enr.next_action_at, utcnow() + timedelta(days=3))
        action = "out of office; next step delayed 3 days"
    elif c.label == C.OPT_OUT:
        add_suppression(db, "EMAIL", prospect.email or reply.from_address, "OPT_OUT", f"reply #{reply.id}")
        add_suppression(db, "EMAIL", reply.from_address, "OPT_OUT", f"reply #{reply.id}")
        if prospect.domain:
            add_suppression(db, "DOMAIN", prospect.domain, "OPT_OUT", f"reply #{reply.id}")
        _stop_enrollments(db, prospect, "opted_out")
        prospect.status, prospect.status_reason = "DO_NOT_CONTACT", "opted out by reply"
        action = "opted out: suppressed email + domain; all sequences stopped"
    elif c.label == C.NOT_INTERESTED:
        add_suppression(db, "EMAIL", prospect.email or reply.from_address, "MANUAL_DNC", f"not interested, reply #{reply.id}")
        _stop_enrollments(db, prospect, "not_interested")
        prospect.status, prospect.status_reason = "DO_NOT_CONTACT", "not interested"
        action = "not interested: suppressed; sequence stopped"
    else:
        _stop_enrollments(db, prospect, "replied")
        prospect.status = "REPLIED"
        if c.label in (C.POSITIVE, C.MEETING) and not c.requires_human:
            meeting = get_or_create_meeting(db, prospect)
            link = f"{get_settings().base_url.rstrip('/')}/book/{meeting.booking_token}"
            body = (f"Thanks for getting back to me! You can pick a time that works for you here: {link}\n\n"
                    f"{get_settings().outreach_sender_name or ''}").strip()
            auto = st.auto_respond_routine
            msg = _reply_message(db, prospect, reply, body, auto=auto, reason="routine reply (auto-respond disabled)")
            action = f"proposed meeting via booking link (reply #{msg.id} {msg.status})"
        elif c.label == C.QUESTION and not c.requires_human:
            faq = match_outreach_faq(db, f"{reply.subject}\n{reply.body}")
            if faq:
                meeting = get_or_create_meeting(db, prospect)
                link = f"{get_settings().base_url.rstrip('/')}/book/{meeting.booking_token}"
                body = f"{faq.answer}\n\nIf it's helpful, you can grab a time to talk here: {link}"
                msg = _reply_message(db, prospect, reply, body, auto=st.auto_respond_routine,
                                     reason="answer drafted from approved company FAQ")
                action = f"answered from approved FAQ #{faq.id} (reply #{msg.id} {msg.status})"
            else:
                reply.requires_human = True
                action = "question not covered by approved information; escalated"
        else:
            reply.requires_human = True
            action = "escalated to operator"
    if reply.requires_human:
        notifications.notify_operator(db, kind="ESCALATION", subject=f"Prospect reply needs you: {c.label}",
                                      body=f"From {reply.from_address}: {reply.body[:500]}",
                                      link="/admin/outreach#replies", dedupe_key=f"reply-esc:{reply.id}")
    reply.handled = True
    reply.action_taken = action
    audit.record(db, "outreach.reply_handled", actor_type="AGENT", entity_type="outreach_reply", entity_id=reply.id,
                 classification=c.label, confidence=c.confidence, action=action)
    return {"classification": c.label, "action": action}


def approve_message(db: Session, msg: OutreachMessage, *, user_id: int, edited_body: str | None = None,
                    edited_subject: str | None = None) -> None:
    if msg.status != "PENDING_APPROVAL":
        raise ValueError("message is not awaiting approval")
    if edited_body is not None:
        msg.body = edited_body
    if edited_subject is not None:
        msg.subject = edited_subject
    msg.status, msg.approved_by_id, msg.approved_at = "APPROVED", user_id, utcnow()
    audit.record(db, "outreach.message_approved", actor_type="USER", actor_user_id=user_id,
                 entity_type="outreach_message", entity_id=msg.id, edited=edited_body is not None)


def reject_message(db: Session, msg: OutreachMessage, *, user_id: int, reason: str) -> None:
    msg.status, msg.block_reasons = "CANCELLED", [f"rejected by operator: {reason}"]
    if msg.enrollment_id:
        enr = db.get(Enrollment, msg.enrollment_id)
        if enr and enr.status == "ACTIVE":
            enr.status, enr.stop_reason = "STOPPED", "draft rejected by operator"
    audit.record(db, "outreach.message_rejected", actor_type="USER", actor_user_id=user_id,
                 entity_type="outreach_message", entity_id=msg.id, reason=reason)
