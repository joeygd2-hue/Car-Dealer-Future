"""Daily dashboard and weekly report, computed from stored records only.

Rules: a draft is not a sent email; a requested meeting is not a booked meeting; SIMULATED
records are counted separately and never mixed into real results; estimates are labelled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import (
    Activity,
    AgentRun,
    CostEntry,
    InboundMessage,
    IntegrationLog,
    Job,
    Meeting,
    OutreachMessage,
    Proposal,
    Prospect,
    Task,
)

POSITIVE = ("interested", "meeting_request", "question", "referral")


def local_day_bounds(day: datetime | None = None) -> tuple[datetime, datetime]:
    tz = ZoneInfo(get_settings().timezone)
    local = (day or utcnow()).replace(tzinfo=timezone.utc).astimezone(tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def _count(db: Session, model, *conds) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*conds)) or 0


def _split(db: Session, model, *conds) -> dict[str, int]:
    """Count real and SIMULATED rows separately."""
    return {"real": _count(db, model, model.simulated.is_(False), *conds),
            "simulated": _count(db, model, model.simulated.is_(True), *conds)}


def stage_entries(db: Session, stage: str, start: datetime, end: datetime, *, simulated: bool | None = False) -> int:
    """Distinct prospects that entered ``stage`` in the window (from the timeline)."""
    q = select(Activity).join(Prospect, Prospect.id == Activity.prospect_id).where(
        Activity.kind == "stage", Activity.created_at >= start, Activity.created_at < end)
    if simulated is not None:
        q = q.where(Prospect.simulated.is_(simulated))
    return len({a.prospect_id for a in db.scalars(q) if (a.data or {}).get("to") == stage})


def daily(db: Session, day: datetime | None = None) -> dict[str, Any]:
    start, end = local_day_bounds(day)
    now = utcnow()
    live = Prospect.merged_into_id.is_(None)
    d: dict[str, Any] = {"day_start_utc": start, "generated_at": now}
    d["new_leads"] = _split(db, Prospect, live, Prospect.discovered_at >= start, Prospect.discovered_at < end)
    d["qualified_today"] = {"real": stage_entries(db, "qualified", start, end, simulated=False),
                            "simulated": stage_entries(db, "qualified", start, end, simulated=True)}
    d["qualified_total"] = _split(db, Prospect, live, Prospect.stage.in_(("qualified", "ready_for_outreach")))
    d["awaiting_approval"] = _split(db, OutreachMessage, OutreachMessage.status == "draft")
    d["queued"] = _split(db, OutreachMessage, OutreachMessage.status.in_(("approved", "queued", "sending")))
    d["sent"] = _split(db, OutreachMessage, OutreachMessage.sent_at >= start, OutreachMessage.sent_at < end)
    d["failed_sends"] = _split(db, OutreachMessage, OutreachMessage.status == "failed",
                               OutreachMessage.updated_at >= start)
    human = InboundMessage.classification.notin_(("auto_reply", "bounce"))
    d["responses"] = _split(db, InboundMessage, human, InboundMessage.received_at >= start, InboundMessage.received_at < end)
    d["positive"] = _split(db, InboundMessage, InboundMessage.classification.in_(POSITIVE),
                           InboundMessage.received_at >= start, InboundMessage.received_at < end)
    d["opt_outs"] = _split(db, InboundMessage, InboundMessage.classification == "opt_out",
                           InboundMessage.received_at >= start, InboundMessage.received_at < end)
    d["unmatched_replies"] = _count(db, InboundMessage, InboundMessage.prospect_id.is_(None), InboundMessage.handled.is_(False))
    d["interested_prospects"] = _split(db, Prospect, live, Prospect.stage.in_(("interested", "meeting_requested")))
    d["tasks_due_today"] = _count(db, Task, Task.status == "open", Task.due_at >= start, Task.due_at < end)
    d["overdue"] = _count(db, Task, Task.status == "open", Task.due_at < min(start, now))
    d["meetings_requested"] = _split(db, Meeting, Meeting.status.in_(("requested", "pending_approval")))
    d["meetings_booked_today"] = {"real": stage_entries(db, "meeting_booked", start, end, simulated=False),
                                  "simulated": stage_entries(db, "meeting_booked", start, end, simulated=True)}
    d["meetings_upcoming"] = _split(db, Meeting, Meeting.status.in_(("booked", "rescheduled")), Meeting.start_at >= now)
    prep_q = select(Meeting).where(Meeting.status.in_(("booked", "rescheduled")), Meeting.start_at >= now,
                                   Meeting.start_at < now + timedelta(hours=48)).order_by(Meeting.start_at)
    d["meetings_needing_prep"] = list(db.scalars(prep_q))
    d["proposals_sent"] = _count(db, Proposal, Proposal.sent_at >= start, Proposal.sent_at < end)
    d["new_clients"] = {"real": stage_entries(db, "won", start, end, simulated=False),
                        "simulated": stage_entries(db, "won", start, end, simulated=True)}
    d["integration_errors"] = list(db.scalars(select(IntegrationLog).where(
        IntegrationLog.status != "ok", IntegrationLog.created_at >= now - timedelta(hours=24))
        .order_by(IntegrationLog.created_at.desc()).limit(20)))
    d["failed_jobs"] = list(db.scalars(select(Job).where(Job.status == "failed", Job.finished_at >= now - timedelta(days=2))
                                       .order_by(Job.finished_at.desc()).limit(20)))
    d["agent_errors"] = [r for r in db.scalars(select(AgentRun).where(AgentRun.started_at >= now - timedelta(hours=24))
                                               .order_by(AgentRun.started_at.desc()).limit(50)) if r.errors]
    return d


def integration_health(db: Session) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(hours=24)
    out = []
    for prov in db.scalars(select(func.distinct(IntegrationLog.provider))):
        last = db.scalar(select(IntegrationLog).where(IntegrationLog.provider == prov).order_by(IntegrationLog.id.desc()))
        ok = _count(db, IntegrationLog, IntegrationLog.provider == prov, IntegrationLog.created_at >= since, IntegrationLog.status == "ok")
        bad = _count(db, IntegrationLog, IntegrationLog.provider == prov, IntegrationLog.created_at >= since, IntegrationLog.status != "ok")
        out.append({"provider": prov, "last_status": last.status, "last_at": last.created_at, "last_detail": last.detail,
                    "simulated": last.simulated, "ok_24h": ok, "errors_24h": bad})
    return sorted(out, key=lambda r: (r["last_status"] == "ok", r["provider"]))


def _rate(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def weekly(db: Session, end: datetime | None = None, *, include_simulated: bool = False) -> dict[str, Any]:
    end = end or utcnow()
    start = end - timedelta(days=7)
    sim = None if include_simulated else False
    pf = [] if include_simulated else [Prospect.simulated.is_(False)]
    mf = [] if include_simulated else [OutreachMessage.simulated.is_(False)]
    inf = [] if include_simulated else [InboundMessage.simulated.is_(False)]
    meet_f = [] if include_simulated else [Meeting.simulated.is_(False)]

    discovered = _count(db, Prospect, Prospect.merged_into_id.is_(None), Prospect.discovered_at >= start,
                        Prospect.discovered_at < end, *pf)
    qualified = stage_entries(db, "qualified", start, end, simulated=sim)
    contacted = db.scalar(select(func.count(func.distinct(OutreachMessage.prospect_id))).where(
        OutreachMessage.sent_at >= start, OutreachMessage.sent_at < end, *mf)) or 0
    sent = _count(db, OutreachMessage, OutreachMessage.sent_at >= start, OutreachMessage.sent_at < end, *mf)
    replied = db.scalar(select(func.count(func.distinct(InboundMessage.prospect_id))).where(
        InboundMessage.received_at >= start, InboundMessage.received_at < end, InboundMessage.prospect_id.is_not(None),
        InboundMessage.classification.notin_(("auto_reply", "bounce")), *inf)) or 0
    positive = db.scalar(select(func.count(func.distinct(InboundMessage.prospect_id))).where(
        InboundMessage.received_at >= start, InboundMessage.received_at < end,
        InboundMessage.classification.in_(POSITIVE), *inf)) or 0
    opt_outs = _count(db, InboundMessage, InboundMessage.classification == "opt_out", InboundMessage.received_at >= start, *inf)
    bounced = _count(db, OutreachMessage, OutreachMessage.status == "bounced", OutreachMessage.sent_at >= start, *mf)
    booked = stage_entries(db, "meeting_booked", start, end, simulated=sim)
    held = _count(db, Meeting, Meeting.status == "completed", Meeting.start_at >= start, Meeting.start_at < end, *meet_f)
    proposals = _count(db, Proposal, Proposal.sent_at >= start, Proposal.sent_at < end)
    won = stage_entries(db, "won", start, end, simulated=sim)

    funnel = [
        {"stage": "Discovered", "count": discovered, "rate": None},
        {"stage": "Qualified", "count": qualified, "rate": _rate(qualified, discovered)},
        {"stage": "Contacted (actually sent)", "count": contacted, "rate": _rate(contacted, qualified)},
        {"stage": "Replied", "count": replied, "rate": _rate(replied, contacted)},
        {"stage": "Positive reply", "count": positive, "rate": _rate(positive, contacted)},
        {"stage": "Meeting booked (provider-confirmed)", "count": booked, "rate": _rate(booked, positive)},
        {"stage": "Meeting held", "count": held, "rate": _rate(held, booked)},
        {"stage": "Proposal sent", "count": proposals, "rate": _rate(proposals, held)},
        {"stage": "Won", "count": won, "rate": _rate(won, proposals)},
    ]

    sources = []
    for (src,) in db.execute(select(func.distinct(Prospect.lead_source)).where(*pf)):
        base = [Prospect.lead_source == src, Prospect.merged_into_id.is_(None), *pf]
        total = _count(db, Prospect, *base)
        q_ = _count(db, Prospect, *base, Prospect.stage.notin_(("new", "researching")))
        pids = select(Prospect.id).where(*base)
        c_ = db.scalar(select(func.count(func.distinct(OutreachMessage.prospect_id))).where(
            OutreachMessage.prospect_id.in_(pids), OutreachMessage.sent_at.is_not(None), *mf)) or 0
        r_ = db.scalar(select(func.count(func.distinct(InboundMessage.prospect_id))).where(
            InboundMessage.prospect_id.in_(pids), InboundMessage.classification.in_(POSITIVE), *inf)) or 0
        mtg = db.scalar(select(func.count(func.distinct(Meeting.prospect_id))).where(
            Meeting.prospect_id.in_(pids), Meeting.status.in_(("booked", "rescheduled", "completed")), *meet_f)) or 0
        w_ = _count(db, Prospect, *base, Prospect.stage == "won")
        spend = float(db.scalar(select(func.coalesce(func.sum(CostEntry.amount), 0)).where(CostEntry.lead_source == src)) or 0)
        sources.append({"source": src, "prospects": total, "qualified": q_, "contacted": c_, "positive": r_,
                        "meetings": mtg, "won": w_, "spend": spend,
                        "cost_per_meeting": round(spend / mtg, 2) if mtg and spend else None})

    spend_actual = float(db.scalar(select(func.coalesce(func.sum(CostEntry.amount), 0)).where(
        CostEntry.incurred_on >= start, CostEntry.incurred_on < end, CostEntry.is_estimate.is_(False),
        CostEntry.client_id.is_(None))) or 0)  # client-attributed costs are delivery, not acquisition
    spend_est = float(db.scalar(select(func.coalesce(func.sum(CostEntry.amount), 0)).where(
        CostEntry.incurred_on >= start, CostEntry.incurred_on < end, CostEntry.is_estimate.is_(True),
        CostEntry.client_id.is_(None))) or 0)  # client-attributed costs are delivery, not acquisition
    cac = round((spend_actual + spend_est) / won, 2) if won and (spend_actual + spend_est) else None

    return {"start": start, "end": end, "include_simulated": include_simulated, "funnel": funnel, "sources": sources,
            "sent": sent, "opt_outs": opt_outs, "bounced": bounced,
            "spend_actual": spend_actual, "spend_estimated": spend_est,
            "cac": cac, "cac_note": None if cac else "Not calculable: needs at least one won client and recorded costs.",
            "meeting_conversion": _rate(booked, positive),
            "recommendations": recommendations(db, funnel, sent, opt_outs, bounced)}


def recommendations(db: Session, funnel, sent: int, opt_outs: int, bounced: int) -> list[str]:
    recs = []
    by = {f["stage"]: f for f in funnel}
    drafts = _count(db, OutreachMessage, OutreachMessage.status == "draft")
    if drafts:
        recs.append(f"{drafts} drafted message(s) are waiting for approval — review the outreach queue.")
    unmatched = _count(db, InboundMessage, InboundMessage.prospect_id.is_(None), InboundMessage.handled.is_(False))
    if unmatched:
        recs.append(f"{unmatched} inbound email(s) could not be matched to a prospect — assign them in the inbox.")
    overdue = _count(db, Task, Task.status == "open", Task.due_at < utcnow())
    if overdue:
        recs.append(f"{overdue} task(s) are overdue.")
    if sent and bounced / sent > 0.05:
        recs.append(f"Bounce rate {100 * bounced / sent:.0f}% is high; verify addresses before sending more.")
    if sent and opt_outs / sent > 0.02:
        recs.append(f"Opt-out rate {100 * opt_outs / sent:.0f}% is high; revisit targeting and message content.")
    if by["Discovered"]["count"] and not by["Qualified"]["count"]:
        recs.append("Leads were found but none qualified; check scoring weights and missing data.")
    pending_meetings = _count(db, Meeting, Meeting.status.in_(("requested", "pending_approval")))
    if pending_meetings:
        recs.append(f"{pending_meetings} meeting request(s) need times offered or approval.")
    missing_email = _count(db, Prospect, Prospect.stage == "qualified", Prospect.email.is_(None))
    if missing_email:
        recs.append(f"{missing_email} qualified prospect(s) have no email; research a verified contact or call.")
    if not recs:
        recs.append("No blockers found in stored data.")
    return recs
