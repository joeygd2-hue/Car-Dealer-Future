"""Persistent autonomous outreach agent.

The agent is not a long-running conversation or loop. The worker process enqueues one
``agent_tick`` job per interval (idempotent key per time bucket). Each tick:

  1. checks the control state (running / paused / emergency_stopped / stopped) and operating hours;
  2. runs a fixed list of bounded steps, each capped by ``config`` limits;
  3. records an ``AgentRun`` with counts and errors and stores a checkpoint.

A step failing does not stop the others. A worker crash leaves the tick job ``running``; the
job runner releases stale locks and the next tick resumes from the stored checkpoint.

Safety properties enforced here and in services.compliance / services.outreach:
- Real providers send nothing until an admin authorizes live operation and
  DF_OUTREACH_SENDING_ENABLED=true. Until then only SIMULATED providers act.
- Messages are auto-approved only for campaigns an admin set to approval_mode="auto", and only
  when rendered purely from an approved template with no review flags.
- Booking a meeting, sensitive replies, low-confidence classifications and unmatched replies
  always go to a person.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit, clear_failures, log_integration, replay_failures
from ..config import get_settings
from ..db import utcnow
from ..integrations.email import get_email_provider
from ..jobs import enqueue, handler
from ..models import (
    AgentRun,
    AgentState,
    Campaign,
    Enrollment,
    Evidence,
    InboundMessage,
    OutreachMessage,
    Prospect,
    Task,
    Template,
    User,
)
from ..security import role_at_least
from . import costs
from .crm import add_activity, advance_stage, create_task, set_stage
from .knowledge import find_answer
from .outreach import ApprovalError, DraftError, approve, create_draft, create_reply_draft, live_sending_allowed
from .scoring import score_prospect
from .sequences import enroll
from .settings_store import get_setting

log = logging.getLogger("dealerflow.agent")

DEFAULT_CONFIG: dict[str, Any] = {
    "tick_minutes": 5,
    "operating_days": [0, 1, 2, 3, 4],  # Monday=0, local time
    "operating_hours": [8, 18],  # sends/drafts only inside this window
    "inbox_poll_minutes": 5,
    "max_score_per_tick": 50,
    "max_enrich_per_tick": 5,
    "max_enroll_per_tick": 20,
    "max_drafts_per_tick": 10,
    "max_auto_replies_per_tick": 5,
    "replenish_below": 15,  # run discovery when fewer eligible prospects than this are waiting
    "discovery_source": "fixture",  # fixture (SIMULATED) | osm
    "discovery_max_runs_per_day": 1,
    "daily_budget_usd": 5.0,
    "monthly_budget_usd": 50.0,
    "job_max_attempts": 4,
    "heartbeat_stale_minutes": 15,
}


class ControlError(ValueError):
    pass


def state(db: Session) -> AgentState:
    st = db.get(AgentState, 1)
    if st is None:
        st = AgentState(id=1, status="stopped", config=dict(DEFAULT_CONFIG), checkpoint={})
        db.add(st)
        db.flush()
    return st


def config(db: Session) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(state(db).config or {})
    return cfg


def update_config(db: Session, changes: dict[str, Any], *, actor: User) -> dict[str, Any]:
    if not role_at_least(actor.role, "admin"):
        raise ControlError("Only an admin can change agent limits")
    unknown = set(changes) - set(DEFAULT_CONFIG)
    if unknown:
        raise ControlError(f"Unknown settings: {', '.join(sorted(unknown))}")
    st = state(db)
    new = dict(config(db))
    for k, v in changes.items():
        default = DEFAULT_CONFIG[k]
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            new[k] = v
        else:
            v = type(default)(v)
            if v < 0:
                raise ControlError(f"{k} cannot be negative")
            new[k] = v
    if new["discovery_source"] not in ("fixture", "osm"):
        raise ControlError("discovery_source must be fixture or osm (Google Places results may not be stored)")
    st.config = new
    audit(db, "agent.config", actor_id=actor.id, data={"changes": changes})
    return new


def set_status(db: Session, status: str, *, actor: User, reason: str = "") -> AgentState:
    st = state(db)
    if status not in ("running", "paused", "stopped", "emergency_stopped"):
        raise ControlError(f"Unknown status {status}")
    if status == "running" and st.status == "emergency_stopped" and not role_at_least(actor.role, "admin"):
        raise ControlError("Only an admin can clear an emergency stop")
    if not role_at_least(actor.role, "manager"):
        raise ControlError("Only a manager or admin can control the agent")
    old = st.status
    st.status, st.status_reason, st.status_changed_by_id, st.status_changed_at = status, reason, actor.id, utcnow()
    if status == "emergency_stopped":
        # Pull every unsent message back to draft so nothing goes out until a person re-approves it.
        n = 0
        for m in db.scalars(select(OutreachMessage).where(OutreachMessage.status.in_(("approved", "queued")))):
            m.status, m.error = "draft", "Returned to draft by emergency stop"
            n += 1
        reason = f"{reason} ({n} queued message(s) returned to draft)".strip()
        st.status_reason = reason
    audit(db, "agent.status", actor_id=actor.id, data={"from": old, "to": status, "reason": reason})
    return st


def authorize_live(db: Session, *, actor: User, authorize: bool) -> AgentState:
    if not role_at_least(actor.role, "admin"):
        raise ControlError("Only an admin can authorize live operation")
    st = state(db)
    st.live_authorized = authorize
    st.live_authorized_by_id = actor.id if authorize else None
    st.live_authorized_at = utcnow() if authorize else None
    audit(db, "agent.live_authorization", actor_id=actor.id, data={"authorized": authorize})
    return st


def set_campaign_mode(db: Session, campaign: Campaign, *, actor: User, approval_mode: str | None = None,
                      status: str | None = None, auto_reply: bool | None = None) -> Campaign:
    if approval_mode is not None:
        if approval_mode not in ("manual", "auto"):
            raise ControlError("approval_mode must be manual or auto")
        if approval_mode == "auto" and not role_at_least(actor.role, "admin"):
            raise ControlError("Only an admin can allow a campaign to send without per-message approval")
        campaign.approval_mode = approval_mode
        campaign.auto_authorized_by_id = actor.id if approval_mode == "auto" else None
        campaign.auto_authorized_at = utcnow() if approval_mode == "auto" else None
    if status is not None:
        if status not in ("active", "paused", "archived"):
            raise ControlError("status must be active, paused or archived")
        campaign.status = status
    if auto_reply is not None:
        if auto_reply and not role_at_least(actor.role, "admin"):
            raise ControlError("Only an admin can enable automatic answers to routine questions")
        campaign.auto_reply_enabled = auto_reply
    audit(db, "campaign.update", actor_id=actor.id, entity_type="campaign", entity_id=campaign.id,
          data={"approval_mode": approval_mode, "status": status, "auto_reply": auto_reply})
    return campaign


def in_operating_window(cfg: dict[str, Any], now_utc: datetime) -> bool:
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(get_settings().timezone))
    start, end = cfg["operating_hours"]
    return local.weekday() in cfg["operating_days"] and start <= local.hour < end


# --- Steps -----------------------------------------------------------------------------------

def step_poll_inbox(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    last = cp.get("inbox_polled_at")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=cfg["inbox_poll_minutes"]):
        return {"skipped": "polled recently"}
    from .inbox import poll_inbox_job
    res = poll_inbox_job(db, {})
    cp.update(state(db).checkpoint or {})
    cp["inbox_polled_at"] = now.isoformat()
    return res


def step_handle_replies(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    """Answer routine questions from approved info; escalate everything else."""
    out = {"drafted": 0, "auto_sent": 0, "escalated": 0}
    rows = list(db.scalars(select(InboundMessage).where(InboundMessage.handled.is_(False),
                                                        InboundMessage.prospect_id.is_not(None))
                           .order_by(InboundMessage.received_at).limit(cfg["max_auto_replies_per_tick"])))
    for row in rows:
        p = row.prospect
        camp = None
        if row.outreach_message_id:
            parent = db.get(OutreachMessage, row.outreach_message_id)
            camp = db.get(Campaign, parent.campaign_id) if parent and parent.campaign_id else None
        if row.classification in ("question", "interested") and not row.escalated and camp and camp.auto_reply_enabled:
            ans = find_answer(db, row.body, p.preferred_language or "en")
            if not ans.escalate:
                msg = create_reply_draft(db, row, ans.text, actor=None, origin="agent",
                                         flags=None)
                out["drafted"] += 1
                add_activity(db, p, "agent", f"Agent drafted answer from approved info: {ans.reason}",
                             ref_type="outreach", ref_id=msg.id)
                if camp.approval_mode == "auto" and _may_auto_send(db, msg):
                    try:
                        approve(db, msg, actor=None, kind="campaign_auto")
                        out["auto_sent"] += 1
                    except ApprovalError as exc:
                        msg.review_flags = (msg.review_flags or []) + [f"auto-approval refused: {exc}"]
                row.handled = True
                continue
            reason = ans.reason
        elif row.classification == "meeting_request":
            reason = "Meeting request: offer real availability and approve the booking"
        else:
            reason = f"{row.classification} (confidence {row.classification_confidence:.2f}) needs a person"
        row.escalated, row.handled = True, True
        out["escalated"] += 1
        create_task(db, f"Reply from {p.name} needs a person: {reason}"[:255], now + timedelta(hours=4), prospect=p,
                    kind="review", dedupe_key=f"escalate:{row.id}")
    return out


def _may_auto_send(db: Session, msg: OutreachMessage) -> bool:
    if msg.review_flags:
        return False
    if get_email_provider().simulated:
        return True  # SIMULATED: nothing leaves the system
    ok, _ = live_sending_allowed(db)
    return ok


def eligible_queue_size(db: Session) -> int:
    enrolled = select(Enrollment.prospect_id)
    return db.scalar(select(func.count()).select_from(Prospect).where(
        Prospect.merged_into_id.is_(None), Prospect.do_not_contact.is_(False),
        Prospect.stage.in_(("qualified", "ready_for_outreach")), Prospect.id.notin_(enrolled))) or 0


def step_replenish(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    waiting = eligible_queue_size(db)
    if waiting >= cfg["replenish_below"]:
        return {"skipped": f"{waiting} eligible prospects waiting"}
    today = now.date().isoformat()
    runs = cp.get("discovery_runs", {})
    if runs.get(today, 0) >= cfg["discovery_max_runs_per_day"]:
        return {"skipped": "daily discovery limit reached"}
    source = cfg["discovery_source"]
    est = costs.estimate(db, f"discovery:{source}")
    ok, why = costs.within_budget(db, est, cfg["daily_budget_usd"], cfg["monthly_budget_usd"])
    if not ok:
        return {"skipped": why}
    from .discovery import run_source
    r = run_source(db, source)
    result: dict[str, Any] = {"source": source, "created": r["created"], "merged": r["merged"]}
    runs = {today: runs.get(today, 0) + 1}  # keep only today's counter
    cp["discovery_runs"] = runs
    return result


def step_enrich(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    if not get_settings().website_research_enabled:
        return {"skipped": "website research disabled"}
    researched = select(Evidence.prospect_id).where(Evidence.source_type == "website")
    rows = list(db.scalars(select(Prospect).where(
        Prospect.website.is_not(None), Prospect.simulated.is_(False), Prospect.do_not_contact.is_(False),
        Prospect.merged_into_id.is_(None), Prospect.id.notin_(researched)).limit(cfg["max_enrich_per_tick"])))
    for p in rows:
        enqueue(db, "enrich_website", f"enrich_website:{p.id}", {"prospect_id": p.id}, max_attempts=2)
    return {"enqueued": len(rows)}


def step_score(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    threshold = float(get_setting(db, "qualification_threshold"))
    rows = list(db.scalars(select(Prospect).where(
        Prospect.merged_into_id.is_(None),
        (Prospect.scored_at.is_(None)) | (Prospect.scored_at < Prospect.updated_at) |
        (Prospect.scored_at < now - timedelta(days=7)),
        Prospect.stage.in_(("new", "researching", "qualified", "ready_for_outreach", "follow_up_later")),
    ).limit(cfg["max_score_per_tick"])))
    qualified = ready = 0
    for p in rows:
        b = score_prospect(db, p)
        if b["eligible"] and b["total"] >= threshold and p.stage in ("new", "researching"):
            advance_stage(db, p, "qualified", reason=f"Score {b['total']} ≥ {threshold}")
            qualified += 1
        if p.stage == "qualified" and (p.email or any(c.verified and c.email for c in p.contacts)):
            advance_stage(db, p, "ready_for_outreach", reason="Qualified with an email route")
            ready += 1
    return {"scored": len(rows), "qualified": qualified, "ready": ready}


def step_enroll(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    n = 0
    for camp in db.scalars(select(Campaign).where(Campaign.status == "active")):
        cities = {c.strip().lower() for c in camp.target_cities.split(",") if c.strip()}
        enrolled = select(Enrollment.prospect_id).where(Enrollment.campaign_id == camp.id)
        q = select(Prospect).where(
            Prospect.stage == "ready_for_outreach", Prospect.do_not_contact.is_(False),
            Prospect.merged_into_id.is_(None), Prospect.score >= camp.min_score, Prospect.id.notin_(enrolled),
        ).order_by(Prospect.score.desc()).limit(cfg["max_enroll_per_tick"] - n)
        for p in db.scalars(q):
            if cities and (p.city or "").lower() not in cities:
                continue
            if enroll(db, camp, p, now=now):
                n += 1
        if n >= cfg["max_enroll_per_tick"]:
            break
    return {"enrolled": n}


def _template_for(db: Session, camp: Campaign, step: dict, p: Prospect) -> Template | None:
    lang = camp.language_mode if camp.language_mode in ("en", "es") else (p.preferred_language or "en")
    tpl = db.scalar(select(Template).where(Template.name == step["template"], Template.language == lang,
                                           Template.active.is_(True)))
    return tpl or db.scalar(select(Template).where(Template.name == step["template"], Template.language == "en",
                                                   Template.active.is_(True)))


def step_sequences(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    out = {"drafted": 0, "auto_approved": 0, "awaiting_approval": 0, "completed": 0, "stopped": 0, "capped": 0}
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    due = list(db.scalars(select(Enrollment).join(Campaign).where(
        Enrollment.status == "active", Campaign.status == "active", Enrollment.next_action_at <= now,
    ).order_by(Enrollment.next_action_at).limit(cfg["max_drafts_per_tick"] * 3)))
    for e in due:
        if out["drafted"] >= cfg["max_drafts_per_tick"]:
            break
        camp, p = e.campaign, e.prospect
        if p.do_not_contact:
            e.status, e.stop_reason, e.stopped_at = "stopped", "do_not_contact", now
            out["stopped"] += 1
            continue
        last = db.scalar(select(OutreachMessage).where(OutreachMessage.prospect_id == p.id,
                                                       OutreachMessage.campaign_id == camp.id)
                         .order_by(OutreachMessage.id.desc()))
        if last and last.status in ("draft", "approved", "queued", "sending"):
            continue  # waiting for approval or sending
        if last and last.status in ("failed", "cancelled", "bounced"):
            e.status, e.stop_reason, e.stopped_at = "stopped", f"previous message {last.status}", now
            out["stopped"] += 1
            continue
        if e.step >= len(camp.steps):
            e.status, e.stop_reason = "completed", "all steps sent"
            out["completed"] += 1
            if p.stage == "contacted":
                set_stage(db, p, "follow_up_later", reason=f"Campaign '{camp.name}' finished without a reply", workflow=True)
                p.follow_up_at = now + timedelta(days=60)
            continue
        if last and last.sent_at and e.step > 0:
            wait_until = last.sent_at + timedelta(days=int(camp.steps[e.step].get("delay_days", 3)))
            if now < wait_until:
                e.next_action_at = wait_until
                continue
        made_today = db.scalar(select(func.count()).select_from(OutreachMessage).where(
            OutreachMessage.campaign_id == camp.id, OutreachMessage.created_at >= start_of_day)) or 0
        if made_today >= camp.daily_cap:
            out["capped"] += 1
            continue
        tpl = _template_for(db, camp, camp.steps[e.step], p)
        if tpl is None:
            e.status, e.stop_reason = "stopped", f"template {camp.steps[e.step]['template']!r} missing"
            out["stopped"] += 1
            continue
        try:
            msg = create_draft(db, p, tpl, actor=None, origin="agent", campaign_id=camp.id, step=e.step)
        except DraftError as exc:
            e.status, e.stop_reason, e.stopped_at = "stopped", f"cannot draft: {exc}"[:120], now
            create_task(db, f"Cannot draft for {p.name}: {exc}"[:255], now + timedelta(days=1), prospect=p,
                        kind="review", dedupe_key=f"draft_error:{e.id}:{e.step}")
            out["stopped"] += 1
            continue
        out["drafted"] += 1
        e.step += 1
        e.next_action_at = now + timedelta(hours=1)
        if camp.approval_mode == "auto" and _may_auto_send(db, msg):
            try:
                approve(db, msg, actor=None, kind="campaign_auto")
                out["auto_approved"] += 1
                continue
            except ApprovalError as exc:
                msg.review_flags = (msg.review_flags or []) + [f"auto-approval refused: {exc}"]
        out["awaiting_approval"] += 1
    return out


def step_follow_ups(db: Session, cfg: dict, cp: dict, now: datetime) -> dict:
    n = 0
    for p in db.scalars(select(Prospect).where(Prospect.stage == "follow_up_later", Prospect.follow_up_at <= now,
                                               Prospect.do_not_contact.is_(False))):
        create_task(db, f"Scheduled follow-up: {p.name}", now, prospect=p, kind="follow_up",
                    dedupe_key=f"follow_up_later:{p.id}:{p.follow_up_at:%Y%m%d}")
        n += 1
    return {"created": n}


Step = Callable[[Session, dict, dict, datetime], dict]
# (name, function, runs while paused?, needs operating window?)
STEPS: list[tuple[str, Step, bool, bool]] = [
    ("poll_inbox", step_poll_inbox, True, False),  # reading replies (and opt-outs) continues while paused
    ("handle_replies", step_handle_replies, False, True),
    ("replenish", step_replenish, False, False),
    ("enrich", step_enrich, False, False),
    ("score", step_score, False, False),
    ("enroll", step_enroll, False, False),
    ("sequences", step_sequences, False, True),
    ("follow_ups", step_follow_ups, True, False),
]


def tick(db: Session, *, now: datetime | None = None) -> AgentRun:
    now = now or utcnow()
    st = state(db)
    cfg = config(db)
    run = AgentRun(started_at=now, status="running", counts={}, errors=[])
    db.add(run)
    st.last_tick_started_at = now
    db.flush()
    if st.status in ("stopped", "emergency_stopped"):
        run.status, run.skipped_reason, run.finished_at = "skipped", f"agent {st.status}", utcnow()
        return run
    paused = st.status == "paused"
    window = in_operating_window(cfg, now)
    cp = dict(st.checkpoint or {})
    counts: dict[str, Any] = {}
    errors: list[dict] = []
    for name, fn, while_paused, needs_window in STEPS:
        if paused and not while_paused:
            counts[name] = {"skipped": "paused"}
            continue
        if needs_window and not window:
            counts[name] = {"skipped": "outside operating hours"}
            continue
        try:
            with db.begin_nested():
                counts[name] = fn(db, cfg, cp, now)
        except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
            errors.append({"step": name, "error": f"{type(exc).__name__}: {exc}"[:500]})
            # The savepoint rollback discarded the provider's log rows; write them again, plus the step failure.
            replay_failures(db)
            log_integration(db, f"agent:{name}", "step", "error", detail=f"{type(exc).__name__}: {exc}")
            log.warning("agent step failed", extra={"step": name, "error": str(exc)})
        else:
            clear_failures(db)
        cp[f"step:{name}"] = now.isoformat()
        st = state(db)
        st.checkpoint = dict(cp)
        db.flush()
    run.counts, run.errors = counts, errors
    run.status = "ok" if not errors else ("partial" if len(errors) < len(STEPS) else "error")
    run.finished_at = utcnow()
    st.last_tick_finished_at = run.finished_at
    return run


@handler("agent_tick")
def agent_tick_job(db: Session, payload: dict) -> dict:
    run = tick(db)
    return {"run_id": run.id, "status": run.status}


def heartbeat(db: Session, worker_id: str) -> None:
    st = state(db)
    st.heartbeat_at, st.worker_id = utcnow(), worker_id


def health(db: Session) -> dict[str, Any]:
    st = state(db)
    cfg = config(db)
    age = (utcnow() - st.heartbeat_at).total_seconds() if st.heartbeat_at else None
    return {
        "agent_status": st.status,
        "live_authorized": st.live_authorized,
        "worker_heartbeat_age_seconds": age,
        "worker_alive": age is not None and age < cfg["heartbeat_stale_minutes"] * 60,
        "last_tick_finished_at": st.last_tick_finished_at.isoformat() if st.last_tick_finished_at else None,
    }


def recover_after_restart(db: Session) -> int:
    """Mark ticks that were running when the worker died as interrupted."""
    n = 0
    for r in db.scalars(select(AgentRun).where(AgentRun.status == "running",
                                               AgentRun.started_at < utcnow() - timedelta(minutes=10))):
        r.status, r.finished_at = "error", utcnow()
        r.errors = (r.errors or []) + [{"step": "worker", "error": "interrupted (worker restarted)"}]
        n += 1
    return n


def open_tasks_due(db: Session, now: datetime) -> int:
    return db.scalar(select(func.count()).select_from(Task).where(Task.status == "open", Task.due_at <= now)) or 0
