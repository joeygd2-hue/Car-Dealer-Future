"""Outreach queue, inbox and suppression list."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..db import get_db
from ..integrations.email import MockEmailProvider
from ..integrations.errors import IntegrationError
from ..jobs import run_job
from ..models import InboundMessage, OutreachMessage, Prospect, Suppression, Template, User
from ..services import crm, outreach
from ..services.compliance import email_blockers, suppress
from ..services.inbox import apply_consequences, poll_inbox_job
from .deps import check_csrf, current_user, flash, render, require

router = APIRouter(dependencies=[Depends(check_csrf)])


@router.get("/outreach")
def outreach_queue(request: Request, status: str = "draft", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(OutreachMessage).order_by(OutreachMessage.id.desc())
    if status:
        q = q.where(OutreachMessage.status == status)
    rows = list(db.scalars(q.limit(200)))
    counts = dict(db.execute(select(OutreachMessage.status, func.count()).group_by(OutreachMessage.status)).all())
    blockers = {m.id: email_blockers(db, m) for m in rows if m.status == "draft"}
    return render(request, "outreach.html", user, rows=rows, status=status, counts=counts, blockers=blockers,
                  sending_enabled=get_settings().outreach_sending_enabled)


@router.post("/prospects/{pid}/draft")
def draft(pid: int, request: Request, template_id: int = Form(...), observation_id: str = Form(""),
          user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = db.get(Prospect, pid)
    tpl = db.get(Template, template_id)
    if not p or not tpl:
        raise HTTPException(404)
    try:
        msg = outreach.create_draft(db, p, tpl, actor=user, observation_evidence_id=int(observation_id) if observation_id else None)
    except outreach.DraftError as exc:
        flash(request, f"Cannot draft: {exc}", "error")
        return RedirectResponse(f"/prospects/{pid}", status_code=303)
    if p.stage in ("new", "researching", "qualified"):
        try:
            crm.set_stage(db, p, "ready_for_outreach", actor=user, reason=f"Draft #{msg.id}")
        except crm.StageError:
            pass
    return RedirectResponse(f"/outreach/{msg.id}", status_code=303)


@router.get("/outreach/{mid}")
def message_detail(mid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, mid)
    if not m:
        raise HTTPException(404)
    return render(request, "message.html", user, m=m, blockers=email_blockers(db, m) if m.status == "draft" else [])


@router.post("/outreach/{mid}/edit")
def message_edit(mid: int, request: Request, subject: str = Form(...), body: str = Form(...),
                 user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, mid)
    if not m or m.status != "draft":
        raise HTTPException(400, "Only drafts can be edited")
    unsub = outreach.unsubscribe_url(m.to_email, m.prospect_id)
    if unsub not in body:
        # A person removed the compliance footer; put it back rather than send without an opt-out.
        body = outreach.with_footer(body.split("\n--\n")[0], m.language, m.to_email, m.prospect_id)
        flash(request, "The unsubscribe footer is required and was restored.", "warn")
    m.subject, m.body = subject.strip()[:255], body
    m.review_flags = [f for f in (m.review_flags or []) if f != "edited by a person"] + ["edited by a person"]
    audit(db, "outreach.edit", actor_id=user.id, entity_type="outreach", entity_id=m.id)
    return RedirectResponse(f"/outreach/{mid}", status_code=303)


@router.post("/outreach/{mid}/approve")
def message_approve(mid: int, request: Request, send_now: str = Form(""), user: User = Depends(require("manager")),
                    db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, mid)
    if not m:
        raise HTTPException(404)
    try:
        outreach.approve(db, m, actor=user)
    except outreach.ApprovalError as exc:
        flash(request, f"Not approved: {exc}", "error")
        return RedirectResponse(f"/outreach/{mid}", status_code=303)
    if send_now:
        db.commit()
        job = outreach.latest_send_job(db, m)
        run_job(job.id)
        db.expire_all()
        m = db.get(OutreachMessage, mid)
    flash(request, {"sent": "Sent" + (" (SIMULATED — nothing left the system)." if m.simulated else "."),
                    "queued": "Approved and queued; the worker will send it within its limits."}.get(
        m.status, f"Status: {m.status}. {m.error or ''}"), "info" if m.status in ("sent", "queued") else "warn")
    return RedirectResponse(f"/outreach/{mid}", status_code=303)


@router.post("/outreach/approve-batch")
async def approve_batch(request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids")][:50]
    ok, bad = 0, []
    for mid in ids:
        m = db.get(OutreachMessage, mid)
        try:
            outreach.approve(db, m, actor=user)
            ok += 1
        except outreach.ApprovalError as exc:
            bad.append(f"#{mid}: {exc}")
    flash(request, f"Approved {ok}." + (" Not approved: " + "; ".join(bad[:5]) if bad else ""), "warn" if bad else "info")
    return RedirectResponse("/outreach", status_code=303)


@router.post("/outreach/{mid}/cancel")
def message_cancel(mid: int, request: Request, reason: str = Form(""), user: User = Depends(require("rep")),
                   db: Session = Depends(get_db)):
    m = db.get(OutreachMessage, mid)
    try:
        outreach.reject(db, m, actor=user, reason=reason or "cancelled")
    except outreach.ApprovalError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/outreach/{mid}", status_code=303)


@router.post("/outreach/{mid}/retry")
def message_retry(mid: int, request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    """Return a failed message to draft so it is re-checked and re-approved (never resent blindly)."""
    m = db.get(OutreachMessage, mid)
    if not m or m.status != "failed":
        raise HTTPException(400, "Only failed messages can be retried")
    m.status, m.error = "draft", None
    audit(db, "outreach.retry", actor_id=user.id, entity_type="outreach", entity_id=m.id)
    flash(request, "Returned to draft. Confirm in your mailbox that it was not delivered before approving again.", "warn")
    return RedirectResponse(f"/outreach/{mid}", status_code=303)


# --- Inbox -------------------------------------------------------------------------------------

@router.get("/inbox")
def inbox(request: Request, show: str = "open", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(InboundMessage).order_by(InboundMessage.received_at.desc())
    if show == "open":
        q = q.where((InboundMessage.handled.is_(False)) | (InboundMessage.escalated.is_(True)))
    elif show == "unmatched":
        q = q.where(InboundMessage.prospect_id.is_(None))
    rows = list(db.scalars(q.limit(200)))
    return render(request, "inbox.html", user, rows=rows, show=show, mock=get_settings().email_provider == "mock")


@router.post("/inbox/poll")
def inbox_poll(request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    try:
        res = poll_inbox_job(db, {})
        flash(request, f"Fetched {res['fetched']}, stored {res['stored']} new.")
    except IntegrationError as exc:
        flash(request, f"Inbox fetch failed: {exc}", "error")
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/simulate")
def inbox_simulate(request: Request, prospect_id: int = Form(...), body: str = Form(...),
                   user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    """Development aid: inject a SIMULATED reply. Only available with the mock email provider."""
    if get_settings().email_provider != "mock":
        raise HTTPException(403, "Simulated replies are only available with the mock email provider")
    p = db.get(Prospect, prospect_id)
    last = db.scalar(select(OutreachMessage).where(OutreachMessage.prospect_id == prospect_id,
                                                   OutreachMessage.sent_at.is_not(None)).order_by(OutreachMessage.sent_at.desc()))
    MockEmailProvider.inject_reply(last.to_email if last else (p.email or "someone@simulated.invalid"),
                                   f"Re: {last.subject}" if last else "Hello", body,
                                   in_reply_to=last.rfc_message_id if last else None,
                                   thread_id=last.thread_id if last else None)
    res = poll_inbox_job(db, {})
    flash(request, f"SIMULATED reply injected and imported ({res['stored']} stored).")
    return RedirectResponse("/inbox", status_code=303)


@router.get("/inbox/{iid}")
def inbound_detail(iid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(InboundMessage, iid)
    if not row:
        raise HTTPException(404)
    return render(request, "inbound.html", user, row=row)


@router.post("/inbox/{iid}/assign")
def inbound_assign(iid: int, request: Request, prospect_id: int = Form(...), user: User = Depends(require("rep")),
                   db: Session = Depends(get_db)):
    row = db.get(InboundMessage, iid)
    p = db.get(Prospect, prospect_id)
    if not row or not p:
        raise HTTPException(404)
    row.prospect_id, row.matched_by, row.escalated = p.id, "manual", False
    db.flush()
    db.refresh(row)
    apply_consequences(db, row)
    audit(db, "inbound.assign", actor_id=user.id, entity_type="inbound", entity_id=iid, data={"prospect": p.id})
    return RedirectResponse(f"/inbox/{iid}", status_code=303)


@router.post("/inbox/{iid}/reclassify")
def inbound_reclassify(iid: int, request: Request, classification: str = Form(...), user: User = Depends(require("rep")),
                       db: Session = Depends(get_db)):
    row = db.get(InboundMessage, iid)
    old = row.classification
    row.classification, row.classification_confidence = classification, 1.0
    row.classification_reasons = (row.classification_reasons or []) + [f"set by {user.email} (was {old})"]
    if row.prospect_id or classification == "opt_out":
        apply_consequences(db, row)
    audit(db, "inbound.reclassify", actor_id=user.id, entity_type="inbound", entity_id=iid, data={"from": old, "to": classification})
    return RedirectResponse(f"/inbox/{iid}", status_code=303)


@router.post("/inbox/{iid}/handled")
def inbound_handled(iid: int, request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    row = db.get(InboundMessage, iid)
    row.handled, row.escalated = True, False
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/{iid}/reply")
def inbound_reply(iid: int, request: Request, body: str = Form(...), user: User = Depends(require("rep")),
                  db: Session = Depends(get_db)):
    row = db.get(InboundMessage, iid)
    try:
        msg = outreach.create_reply_draft(db, row, body, actor=user)
    except outreach.DraftError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/inbox/{iid}", status_code=303)
    row.handled = True
    return RedirectResponse(f"/outreach/{msg.id}", status_code=303)


# --- Suppression -------------------------------------------------------------------------------

@router.get("/suppression")
def suppression_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Suppression).order_by(Suppression.created_at.desc()).limit(1000)))
    return render(request, "suppression.html", user, rows=rows)


@router.post("/suppression")
def suppression_add(request: Request, value: str = Form(...), reason: str = Form("manual"),
                    user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    suppress(db, value, reason, source=f"added by {user.email}", actor_id=user.id)
    flash(request, f"Suppressed {value}.")
    return RedirectResponse("/suppression", status_code=303)


