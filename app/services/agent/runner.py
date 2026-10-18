"""The agent tick: one bounded, checkpointed unit of autonomous work.

A scheduled task enqueues `agent.tick` every few minutes. Each tick:
  1. exits immediately unless state == running;
  2. replenishes the prospect queue from configured permitted sources when low;
  3. researches/qualifies a small batch of new prospects;
  4. enrolls qualified prospects into active campaigns;
  5. drafts due sequence steps (approval policy applied);
  6. sends approved messages through the compliance gate (limits apply);
  7. polls the inbox and handles replies.
State is re-checked between steps so a pause or emergency stop takes effect
within the current tick. Every step's outcome is logged to agent_activity.
All work items are idempotent, so a crash mid-tick is safe to retry.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations import inbox
from app.models.base import utcnow
from app.models.outreach import AgentActivity, Campaign, Enrollment, InboundMessage, OutreachMessage, Prospect
from app.services.agent import compliance, discovery, enrichment, messaging
from app.services.agent.state import get_config, log_activity
from app.services.jobs import handler


def _running(db: Session) -> bool:
    db.expire_all()
    return get_config(db).state == "running"


def tick(db: Session, job_id: int | None = None, http_client=None, sleep=None) -> dict:
    cfg = get_config(db)
    summary: dict = {"state": cfg.state}
    if cfg.state != "running":
        log_activity(db, "agent.tick", "skipped", job_id=job_id, detail=f"agent {cfg.state}")
        return summary
    budget = cfg.tick_action_limit

    steps = [
        ("replenish", lambda: _replenish(db, cfg, http_client)),
        ("research", lambda: _research(db, cfg, http_client, sleep)),
        ("enroll", lambda: _enroll(db, cfg)),
        ("draft", lambda: _draft_due(db, cfg)),
        ("send", lambda: _send_approved(db, cfg, job_id)),
        ("inbox", lambda: _poll_inbox(db, cfg)),
        ("handle", lambda: _handle_inbound(db)),
    ]
    for name, fn in steps:
        if not _running(db):
            summary["halted_before"] = name
            log_activity(db, "agent.tick", "skipped", job_id=job_id, detail=f"halted before {name}: state changed")
            break
        if budget <= 0 and name in ("research", "draft", "send"):
            summary[name] = "action limit reached"
            continue
        try:
            result = fn()
            summary[name] = result
            if isinstance(result, int):
                budget -= result
            db.commit()  # checkpoint after each step
        except Exception as exc:  # noqa: BLE001 — record and continue with the next step
            db.rollback()
            summary[name] = f"error: {type(exc).__name__}"
            log_activity(db, f"agent.{name}", "error", job_id=job_id, detail=f"{type(exc).__name__}: {exc}")
            db.commit()
    cfg = get_config(db)
    cfg.last_tick_at, cfg.last_tick_summary = utcnow(), {k: v for k, v in summary.items() if isinstance(v, (int, str))}
    log_activity(db, "agent.tick", "ok", job_id=job_id, data=cfg.last_tick_summary)
    db.commit()
    return summary


def _replenish(db: Session, cfg, http_client) -> int | str:
    queue = db.scalar(select(func.count()).select_from(Prospect).where(Prospect.stage.in_(("new", "qualified")))) or 0
    if queue >= cfg.min_qualified_queue:
        return "queue sufficient"
    if not compliance.budget_ok(db, cfg):
        log_activity(db, "agent.replenish", "blocked", detail="daily budget exhausted")
        return "budget exhausted"
    if not cfg.discovery_queries:
        return "no discovery queries"
    # Rotate through queries, one per tick, based on how many discovery runs have happened.
    runs = db.scalar(select(func.count()).select_from(AgentActivity).where(AgentActivity.action == "agent.discovery")) or 0
    q = cfg.discovery_queries[runs % len(cfg.discovery_queries)]
    res = discovery.places_search(db, q, client=http_client)
    log_activity(db, "agent.discovery", "skipped" if "skipped" in res else "ok", detail=f"query '{q}'", data=res)
    return res.get("created", 0) if "skipped" not in res else res["skipped"]


def _research(db: Session, cfg, http_client, sleep) -> int:
    batch = db.scalars(
        select(Prospect).where(Prospect.stage == "new", Prospect.research_attempts < cfg.max_retries).order_by(Prospect.created_at).limit(cfg.research_batch_size)
    ).all()
    n = 0
    for p in batch:
        if not _running(db):
            break
        try:
            enrichment.enrich(db, p, http_client=http_client, sleep=sleep)
            log_activity(db, "prospect.researched", "ok", prospect_id=p.id, to_state=p.stage, detail=p.disqualify_reason or p.qualification_notes)
        except Exception as exc:  # noqa: BLE001
            p.stage = "new"  # retry later, bounded by research_attempts
            log_activity(db, "prospect.researched", "error", prospect_id=p.id, detail=str(exc)[:500])
        db.commit()
        n += 1
    return n


def _enroll(db: Session, cfg) -> int:
    campaigns = db.scalars(select(Campaign).where(Campaign.status == "active")).all()
    n = 0
    for c in campaigns:
        enrolled_today = db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.campaign_id == c.id, Enrollment.created_at >= utcnow().replace(hour=0, minute=0, second=0))) or 0
        room = max(0, c.daily_limit - enrolled_today)
        if room == 0:
            continue
        stmt = (
            select(Prospect)
            .where(Prospect.stage == "qualified", Prospect.email.is_not(None), ~Prospect.id.in_(select(Enrollment.prospect_id)))
            .order_by(Prospect.priority.desc())
            .limit(room)
        )
        if c.target_cities:
            stmt = stmt.where(func.lower(Prospect.city).in_([x.lower() for x in c.target_cities]))
        for p in db.scalars(stmt):
            if compliance.is_suppressed(db, email=p.email, phone=p.phone):
                log_activity(db, "prospect.enroll", "blocked", prospect_id=p.id, campaign_id=c.id, detail="suppressed")
                continue
            db.add(Enrollment(prospect_id=p.id, campaign_id=c.id, status="active", next_step=0, next_action_at=utcnow()))
            log_activity(db, "prospect.enrolled", "ok", prospect_id=p.id, campaign_id=c.id)
            n += 1
    return n


def _draft_due(db: Session, cfg) -> int:
    due = db.scalars(
        select(Enrollment).join(Campaign).where(Enrollment.status == "active", Enrollment.next_action_at <= utcnow(), Campaign.status == "active").limit(cfg.tick_action_limit)
    ).all()
    n = 0
    for enr in due:
        if enr.next_step > cfg.max_followups:
            enr.status, enr.stop_reason, enr.stopped_at = "completed", "max follow-ups reached", utcnow()
            continue
        pending = db.scalar(select(OutreachMessage.id).where(OutreachMessage.enrollment_id == enr.id, OutreachMessage.step_index == enr.next_step))
        if pending:
            continue
        try:
            if messaging.draft_step(db, enr):
                n += 1
        except messaging.MessagingError as exc:
            log_activity(db, "message.draft", "blocked", prospect_id=enr.prospect_id, campaign_id=enr.campaign_id, detail=str(exc))
            enr.next_action_at = None  # needs human attention
    return n


def _send_approved(db: Session, cfg, job_id) -> int:
    msgs = db.scalars(select(OutreachMessage).where(OutreachMessage.status == "approved").order_by(OutreachMessage.kind.desc(), OutreachMessage.id).limit(cfg.tick_action_limit)).all()
    sent = 0
    for m in msgs:
        if not _running(db):
            break
        outcome = messaging.send_message(db, m, job_id)
        db.commit()
        if outcome in ("sent", "sandboxed"):
            sent += 1
        elif outcome == "deferred" and m.kind != "reply":
            # A limit or schedule block applies to everything else in this batch too.
            break
    return sent


def _poll_inbox(db: Session, cfg) -> int:
    provider, items = inbox.fetch_new(db, cfg.live_mode)
    n = 0
    for raw in items:
        _, new = messaging.ingest_inbound(
            db, provider=provider, provider_message_id=raw.provider_message_id, from_address=raw.from_address,
            subject=raw.subject, body=raw.body, in_reply_to=raw.in_reply_to, to_addresses=raw.to_addresses, headers=raw.headers,
        )
        n += new
    return n


def _handle_inbound(db: Session) -> int:
    n = 0
    for msg in db.scalars(select(InboundMessage).where(InboundMessage.handled_at.is_(None)).order_by(InboundMessage.id).limit(50)):
        messaging.handle_inbound(db, msg)
        n += 1
    return n


@handler("agent.tick")
def _job_tick(db: Session, job) -> dict:
    s = tick(db, job_id=job.id)
    return {k: v for k, v in s.items() if isinstance(v, (int, str))}


@handler("agent.send_message")
def _job_send(db: Session, job) -> dict:
    m = db.get(OutreachMessage, job.payload["message_id"])
    if m is None:
        return {"skipped": "missing"}
    return {"outcome": messaging.send_message(db, m, job.id)}
