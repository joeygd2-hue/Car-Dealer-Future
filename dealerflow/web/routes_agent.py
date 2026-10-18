"""Operator console for the autonomous agent."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, utcnow
from ..models import AgentRun, Campaign, Enrollment, InboundMessage, Job, OutreachMessage, Prospect, User
from ..services import agent, costs, reports
from ..services.outreach import live_sending_allowed
from .deps import check_csrf, current_user, flash, render, require, back_to

router = APIRouter(dependencies=[Depends(check_csrf)])


def _rate(n, d):
    return round(100 * n / d, 1) if d else None


def agent_metrics(db: Session) -> dict:
    real = OutreachMessage.simulated.is_(False)
    sent = db.scalar(select(func.count()).select_from(OutreachMessage).where(OutreachMessage.sent_at.is_not(None), real)) or 0
    sent_sim = db.scalar(select(func.count()).select_from(OutreachMessage).where(
        OutreachMessage.sent_at.is_not(None), OutreachMessage.simulated.is_(True))) or 0
    human = InboundMessage.classification.notin_(("auto_reply", "bounce"))
    rr = InboundMessage.simulated.is_(False)
    replies = db.scalar(select(func.count(func.distinct(InboundMessage.prospect_id))).where(human, rr)) or 0
    positive = db.scalar(select(func.count(func.distinct(InboundMessage.prospect_id))).where(
        InboundMessage.classification.in_(reports.POSITIVE), rr)) or 0
    opt_outs = db.scalar(select(func.count()).select_from(InboundMessage).where(InboundMessage.classification == "opt_out", rr)) or 0
    contacted = db.scalar(select(func.count(func.distinct(OutreachMessage.prospect_id))).where(
        OutreachMessage.sent_at.is_not(None), real)) or 0
    from ..models import Meeting, Task
    booked = db.scalar(select(func.count()).select_from(Meeting).where(
        Meeting.status.in_(("booked", "rescheduled", "completed")), Meeting.simulated.is_(False))) or 0
    researched = db.scalar(select(func.count()).select_from(Prospect).where(
        Prospect.scored_at.is_not(None), Prospect.simulated.is_(False))) or 0
    return {
        "researched": researched, "sent": sent, "sent_simulated": sent_sim, "contacted": contacted,
        "replies": replies, "positive": positive, "opt_outs": opt_outs, "meetings_booked": booked,
        "follow_ups_due": db.scalar(select(func.count()).select_from(Task).where(Task.status == "open", Task.due_at <= utcnow())) or 0,
        "reply_rate": _rate(replies, contacted), "positive_rate": _rate(positive, contacted),
        "meeting_rate": _rate(booked, positive),
        "cost_today": costs.spent_today(db), "cost_month": costs.spent_month(db),
        "eligible_queue": agent.eligible_queue_size(db),
    }


@router.get("/agent")
def agent_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    st = agent.state(db)
    live_ok, live_why = live_sending_allowed(db)
    queue = list(db.execute(select(Job.kind, Job.status, func.count()).where(Job.status.in_(("pending", "running", "failed")))
                            .group_by(Job.kind, Job.status)).all())
    runs = list(db.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(20)))
    camps = list(db.scalars(select(Campaign).order_by(Campaign.name)))
    enroll_counts = dict(db.execute(select(Enrollment.campaign_id, func.count()).where(Enrollment.status == "active")
                                    .group_by(Enrollment.campaign_id)).all())
    return render(request, "agent.html", user, st=st, cfg=agent.config(db), health=agent.health(db), live_ok=live_ok,
                  live_why=live_why, queue=queue, runs=runs, campaigns=camps, enroll_counts=enroll_counts,
                  m=agent_metrics(db), integrations=reports.integration_health(db),
                  in_window=agent.in_operating_window(agent.config(db), utcnow()),
                  sending_env=get_settings().outreach_sending_enabled)


@router.post("/agent/status")
def agent_status(request: Request, status: str = Form(...), reason: str = Form(""), user: User = Depends(require("manager")),
                 db: Session = Depends(get_db)):
    try:
        agent.set_status(db, status, actor=user, reason=reason)
        flash(request, f"Agent is now {status.replace('_', ' ')}.", "warn" if status == "emergency_stopped" else "info")
    except agent.ControlError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/agent", status_code=303)


@router.post("/agent/live")
def agent_live(request: Request, authorize: str = Form(...), confirm: str = Form(""), user: User = Depends(require("admin")),
               db: Session = Depends(get_db)):
    if authorize == "1" and confirm != "AUTHORIZE LIVE":
        flash(request, "Type AUTHORIZE LIVE to confirm.", "error")
        return RedirectResponse("/agent", status_code=303)
    agent.authorize_live(db, actor=user, authorize=authorize == "1")
    flash(request, "Live operation authorized." if authorize == "1" else "Live operation revoked.", "warn")
    return RedirectResponse("/agent", status_code=303)


@router.post("/agent/config")
async def agent_config(request: Request, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    form = await request.form()
    changes = {}
    for k, default in agent.DEFAULT_CONFIG.items():
        if k not in form:
            continue
        raw = str(form[k]).strip()
        if isinstance(default, list):
            items = [x.strip() for x in raw.split(",") if x.strip()]
            changes[k] = [int(x) for x in items] if default and isinstance(default[0], int) else items
        else:
            changes[k] = raw
    try:
        agent.update_config(db, changes, actor=user)
        flash(request, "Agent limits saved.")
    except (agent.ControlError, ValueError) as exc:
        flash(request, f"Not saved: {exc}", "error")
    return RedirectResponse("/agent", status_code=303)


@router.post("/agent/tick")
def agent_tick_now(request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    run = agent.tick(db)
    flash(request, f"Tick {run.status}" + (f": {run.skipped_reason}" if run.skipped_reason else "") +
          (f"; errors: {run.errors}" if run.errors else ""), "warn" if run.errors else "info")
    return RedirectResponse("/agent", status_code=303)


@router.get("/agent/runs/{rid}")
def agent_run(rid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(AgentRun, rid)
    if not run:
        raise HTTPException(404)
    return render(request, "agent_run.html", user, run=run)


@router.post("/campaigns/{cid}")
def campaign_update(cid: int, request: Request, status: str = Form(None), approval_mode: str = Form(None),
                    auto_reply: str = Form(None), user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    c = db.get(Campaign, cid)
    if not c:
        raise HTTPException(404)
    try:
        agent.set_campaign_mode(db, c, actor=user, status=status, approval_mode=approval_mode,
                                auto_reply=None if auto_reply is None else auto_reply == "1")
    except agent.ControlError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(back_to(request, "/agent"), status_code=303)


