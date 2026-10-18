"""CRM pages: dashboard, prospects, import/discovery, tasks, meetings, calls, reports."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..audit import audit, track_integration
from ..config import get_settings
from ..db import get_db, utcnow
from ..integrations.calls import get_call_provider
from ..integrations.errors import IntegrationError
from ..jobs import enqueue, run_job
from ..models import (
    DEALERSHIP_TYPES,
    Activity,
    Call,
    Evidence,
    Job,
    Meeting,
    OutreachMessage,
    Proposal,
    Prospect,
    Task,
    Template,
    User,
)
from ..services import crm, importer, meetings, reports
from ..services.compliance import calling_window_ok, is_suppressed, suppress
from ..services.discovery import add_verified_contact, run_source
from ..services.normalize import missing_fields
from ..services.prospects import LeadRecord, intake, merge_prospects
from ..services.scoring import score_prospect
from .deps import check_csrf, current_user, flash, render, require, back_to

router = APIRouter(dependencies=[Depends(check_csrf)])
PAGE_SIZE = 25
BULK_LIMIT = 200
SORTS = {"score": Prospect.score.desc().nulls_last(), "name": Prospect.name.asc(), "discovered": Prospect.discovered_at.desc(),
         "updated": Prospect.updated_at.desc(), "city": Prospect.city.asc()}


def _back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(back_to(request, default), status_code=303)


def _prospect(db: Session, pid: int) -> Prospect:
    p = db.get(Prospect, pid)
    if not p:
        raise HTTPException(404, "Prospect not found")
    return p


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "dashboard.html", user, d=reports.daily(db), health=reports.integration_health(db))


# --- Prospects ---------------------------------------------------------------------------------

def _filtered(db: Session, q: str, stage: str, city: str, dtype: str, source: str, min_score: str, simulated: str):
    stmt = select(Prospect).where(Prospect.merged_into_id.is_(None))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Prospect.name.ilike(like), Prospect.domain.ilike(like), Prospect.email.ilike(like),
                              Prospect.phone.ilike(like)))
    if stage:
        stmt = stmt.where(Prospect.stage == stage)
    if city:
        stmt = stmt.where(Prospect.city.ilike(city))
    if dtype:
        stmt = stmt.where(Prospect.dealership_type == dtype)
    if source:
        stmt = stmt.where(Prospect.lead_source == source)
    if min_score:
        try:
            stmt = stmt.where(Prospect.score >= float(min_score))
        except ValueError:
            pass
    if simulated == "hide":
        stmt = stmt.where(Prospect.simulated.is_(False))
    elif simulated == "only":
        stmt = stmt.where(Prospect.simulated.is_(True))
    return stmt


@router.get("/prospects")
def prospects(request: Request, q: str = "", stage: str = "", city: str = "", dtype: str = "", source: str = "",
              min_score: str = "", simulated: str = "", sort: str = "score", page: int = 1,
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = _filtered(db, q, stage, city, dtype, source, min_score, simulated)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    page = max(1, page)
    rows = list(db.scalars(stmt.order_by(SORTS.get(sort, SORTS["score"]), Prospect.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
    cities = [c for (c,) in db.execute(select(func.distinct(Prospect.city)).where(Prospect.city.is_not(None)).order_by(Prospect.city))]
    sources = [s for (s,) in db.execute(select(func.distinct(Prospect.lead_source)).order_by(Prospect.lead_source))]
    params = dict(q=q, stage=stage, city=city, dtype=dtype, source=source, min_score=min_score, simulated=simulated, sort=sort)
    return render(request, "prospects.html", user, rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
                  stages=crm.stages(db), labels=crm.stage_labels(db), cities=cities, sources=sources,
                  dtypes=DEALERSHIP_TYPES, params=params, bulk_limit=BULK_LIMIT,
                  qs="&".join(f"{k}={v}" for k, v in params.items() if v))


@router.post("/prospects/bulk")
async def prospects_bulk(request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids")]
    action, value, confirm = form.get("action", ""), form.get("value", ""), form.get("confirm", "")
    if not ids:
        flash(request, "Select at least one prospect.", "warn")
        return _back(request, "/prospects")
    if len(ids) > BULK_LIMIT:
        flash(request, f"Bulk actions are limited to {BULK_LIMIT} prospects at a time.", "error")
        return _back(request, "/prospects")
    if confirm != str(len(ids)):
        flash(request, f"Type the number of selected prospects ({len(ids)}) to confirm a bulk action.", "warn")
        return _back(request, "/prospects")
    rows = list(db.scalars(select(Prospect).where(Prospect.id.in_(ids))))
    done, errors = 0, []
    for p in rows:
        try:
            if action == "stage":
                if value in crm.WORKFLOW_ONLY:
                    raise crm.StageError(f"'{value}' is set by its workflow, not in bulk")
                done += crm.set_stage(db, p, value, actor=user, reason="bulk")
            elif action == "assign":
                p.owner_id = int(value) if value else user.id
                done += 1
            elif action == "rescore":
                score_prospect(db, p)
                done += 1
            elif action == "dnc":
                if p.email:
                    suppress(db, p.email, "manual", source=f"bulk by {user.email}", actor_id=user.id)
                done += crm.set_stage(db, p, "do_not_contact", actor=user, reason="bulk do-not-contact")
            else:
                raise ValueError("unknown action")
        except (crm.StageError, ValueError) as exc:
            errors.append(f"{p.name}: {exc}")
    audit(db, "prospect.bulk", actor_id=user.id, data={"action": action, "value": value, "ids": ids, "done": done})
    flash(request, f"{action}: {done} updated." + (f" {len(errors)} skipped: " + "; ".join(errors[:5]) if errors else ""),
          "error" if errors and not done else "info")
    return _back(request, "/prospects")


@router.get("/prospects/new")
def prospect_new(request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    return render(request, "prospect_new.html", user, dtypes=DEALERSHIP_TYPES)


@router.post("/prospects/new")
def prospect_create(request: Request, name: str = Form(...), website: str = Form(""), address_line: str = Form(""),
                    city: str = Form(""), state: str = Form("TX"), postal_code: str = Form(""), phone: str = Form(""),
                    email: str = Form(""), dealership_type: str = Form("unknown"), inventory_url: str = Form(""),
                    source_url: str = Form(""), notes: str = Form(""),
                    user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    rec = LeadRecord(name=name, source="manual", source_url=source_url or None, website=website or None,
                     address_line=address_line or None, city=city or None, state=state or None,
                     postal_code=postal_code or None, phone=phone or None, email=email or None,
                     dealership_type=dealership_type, inventory_url=inventory_url or None, notes=notes)
    res = intake(db, rec, actor_id=user.id)
    if res.prospect is None:
        flash(request, "Not saved: " + "; ".join(res.errors), "error")
        return RedirectResponse("/prospects/new", status_code=303)
    msg = "Created." if res.created else f"Matched existing prospect ({res.matched_by}); new details merged."
    if res.errors:
        msg += " Dropped invalid values: " + "; ".join(res.errors)
    flash(request, msg, "warn" if res.errors else "info")
    return RedirectResponse(f"/prospects/{res.prospect.id}", status_code=303)


@router.get("/prospects/{pid}")
def prospect_detail(pid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    if p.merged_into_id:
        return RedirectResponse(f"/prospects/{p.merged_into_id}", status_code=303)
    messages = list(db.scalars(select(OutreachMessage).where(OutreachMessage.prospect_id == p.id).order_by(OutreachMessage.id.desc())))
    tasks = list(db.scalars(select(Task).where(Task.prospect_id == p.id).order_by(Task.status, Task.due_at)))
    mtgs = list(db.scalars(select(Meeting).where(Meeting.prospect_id == p.id).order_by(Meeting.id.desc())))
    props = list(db.scalars(select(Proposal).where(Proposal.prospect_id == p.id)))
    calls = list(db.scalars(select(Call).where(Call.prospect_id == p.id).order_by(Call.id.desc())))
    tpls = list(db.scalars(select(Template).where(Template.active.is_(True)).order_by(Template.name, Template.language)))
    users = list(db.scalars(select(User).where(User.active.is_(True))))
    return render(request, "prospect.html", user, p=p, messages=messages, tasks=tasks, meetings=mtgs, proposals=props,
                  calls=calls, templates_=tpls, users=users, stages=crm.stages(db), labels=crm.stage_labels(db),
                  missing=missing_fields(p), dtypes=DEALERSHIP_TYPES, workflow_only=crm.WORKFLOW_ONLY,
                  suppressed=is_suppressed(db, p.email) if p.email else None,
                  evidence=sorted(p.evidence, key=lambda e: e.collected_at, reverse=True))


@router.post("/prospects/{pid}/edit")
def prospect_edit(pid: int, request: Request, dealership_type: str = Form(...), preferred_language: str = Form("en"),
                  owner_id: str = Form(""), research_notes: str = Form(""), follow_up_at: str = Form(""),
                  user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    if dealership_type not in DEALERSHIP_TYPES or preferred_language not in ("en", "es"):
        raise HTTPException(400, "invalid value")
    changes = {}
    if dealership_type != p.dealership_type:
        changes["dealership_type"] = [p.dealership_type, dealership_type]
        p.dealership_type = dealership_type
        db.add(Evidence(prospect=p, field="dealership_type", value=dealership_type, source_type="manual",
                        verified=True, verified_by_id=user.id, note=f"Set by {user.email}"))
    p.preferred_language = preferred_language
    p.owner_id = int(owner_id) if owner_id else None
    p.research_notes = research_notes
    p.follow_up_at = datetime.fromisoformat(follow_up_at) if follow_up_at else None
    audit(db, "prospect.edit", actor_id=user.id, entity_type="prospect", entity_id=p.id, data=changes)
    flash(request, "Saved.")
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/stage")
def prospect_stage(pid: int, request: Request, stage: str = Form(...), reason: str = Form(""),
                   user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    try:
        crm.set_stage(db, p, stage, actor=user, reason=reason)
        if stage == "do_not_contact" and p.email:
            suppress(db, p.email, "manual", source=f"stage set by {user.email}", actor_id=user.id)
    except crm.StageError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/note")
def prospect_note(pid: int, request: Request, kind: str = Form("note"), body: str = Form(...),
                  user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    if kind not in ("note", "research", "call", "proposal"):
        kind = "note"
    crm.add_activity(db, p, kind, body.strip().splitlines()[0][:200] if body.strip() else kind, body=body, actor_id=user.id)
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/contact")
def prospect_contact(pid: int, request: Request, name: str = Form(...), role: str = Form(""), email: str = Form(""),
                     phone: str = Form(""), source_url: str = Form(...),
                     user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    try:
        add_verified_contact(db, p, name=name, role=role, email=email or None, phone=phone or None,
                             source_url=source_url, actor_id=user.id)
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/evidence/{eid}/verify")
def evidence_verify(eid: int, request: Request, verified: str = Form("1"), note: str = Form(""),
                    user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    ev = db.get(Evidence, eid)
    if not ev:
        raise HTTPException(404)
    ev.verified = verified == "1"
    ev.verified_by_id = user.id if ev.verified else None
    if note:
        ev.note = note
    audit(db, "evidence.verify", actor_id=user.id, entity_type="evidence", entity_id=eid, data={"verified": ev.verified})
    return RedirectResponse(f"/prospects/{ev.prospect_id}#evidence", status_code=303)


@router.post("/prospects/{pid}/rescore")
def prospect_rescore(pid: int, request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    score_prospect(db, _prospect(db, pid))
    return RedirectResponse(f"/prospects/{pid}#score", status_code=303)


@router.post("/prospects/{pid}/enrich")
def prospect_enrich(pid: int, request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    if not get_settings().website_research_enabled:
        flash(request, "Website research is disabled (DF_WEBSITE_RESEARCH_ENABLED).", "warn")
    else:
        job, _ = enqueue(db, "enrich_website", f"enrich_website:{p.id}:{utcnow():%Y%m%d}", {"prospect_id": p.id, "actor_id": user.id})
        flash(request, f"Website research queued (job #{job.id}).")
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/merge")
def prospect_merge(pid: int, request: Request, other_id: int = Form(...), user: User = Depends(require("manager")),
                   db: Session = Depends(get_db)):
    keep, drop = _prospect(db, pid), _prospect(db, other_id)
    try:
        merge_prospects(db, keep, drop, actor_id=user.id)
        flash(request, f"Merged #{drop.id} into this record.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/task")
def prospect_task(pid: int, request: Request, title: str = Form(...), due: str = Form(...), kind: str = Form("follow_up"),
                  user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    due_local = datetime.fromisoformat(due).replace(tzinfo=ZoneInfo(get_settings().timezone))
    crm.create_task(db, title, due_local.astimezone(timezone.utc).replace(tzinfo=None), prospect=p, kind=kind,
                    created_by_id=user.id)
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/prospects/{pid}/proposal")
def prospect_proposal(pid: int, request: Request, title: str = Form(...), monthly_amount: str = Form(""),
                      setup_amount: str = Form(""), status: str = Form("sent"), notes: str = Form(""),
                      user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    pr = Proposal(prospect_id=p.id, title=title, monthly_amount=float(monthly_amount) if monthly_amount else None,
                  setup_amount=float(setup_amount) if setup_amount else None, status=status, notes=notes,
                  sent_at=utcnow() if status == "sent" else None)
    db.add(pr)
    db.flush()
    crm.add_activity(db, p, "proposal", f"Proposal {status}: {title}", actor_id=user.id, ref_type="proposal", ref_id=pr.id)
    if status == "sent":
        try:
            crm.set_stage(db, p, "proposal_sent", actor=user, reason=f"Proposal #{pr.id}")
        except crm.StageError as exc:
            flash(request, str(exc), "warn")
    return RedirectResponse(f"/prospects/{pid}", status_code=303)


@router.post("/proposals/{prid}/status")
def proposal_status(prid: int, request: Request, status: str = Form(...), user: User = Depends(require("rep")),
                    db: Session = Depends(get_db)):
    pr = db.get(Proposal, prid)
    if not pr or status not in ("draft", "sent", "accepted", "rejected"):
        raise HTTPException(400)
    pr.status = status
    if status == "sent" and not pr.sent_at:
        pr.sent_at = utcnow()
    if status in ("accepted", "rejected"):
        pr.decided_at = utcnow()
        try:
            crm.set_stage(db, pr.prospect, "won" if status == "accepted" else "lost", actor=user, reason=f"Proposal #{pr.id} {status}")
        except crm.StageError as exc:
            flash(request, str(exc), "warn")
    crm.add_activity(db, pr.prospect_id, "proposal", f"Proposal {status}: {pr.title}", actor_id=user.id)
    return RedirectResponse(f"/prospects/{pr.prospect_id}", status_code=303)


# --- Calls -------------------------------------------------------------------------------------

@router.post("/prospects/{pid}/call")
def prospect_call(pid: int, request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    if not p.phone_e164:
        flash(request, "No valid phone number on record.", "error")
        return RedirectResponse(f"/prospects/{pid}", status_code=303)
    if p.do_not_contact or is_suppressed(db, phone=p.phone_e164):
        flash(request, "Prospect is do-not-contact; calling is blocked.", "error")
        return RedirectResponse(f"/prospects/{pid}", status_code=303)
    local = utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(get_settings().timezone))
    ok, why = calling_window_ok(local)
    if not ok:
        flash(request, why, "error")
        return RedirectResponse(f"/prospects/{pid}", status_code=303)
    prov = get_call_provider()
    call = Call(prospect_id=p.id, user_id=user.id, to_number=p.phone_e164, provider=prov.name, simulated=prov.simulated)
    db.add(call)
    db.flush()
    try:
        with track_integration(db, prov.name, "start_call", simulated=prov.simulated):
            res = prov.start_call(p.phone_e164, call_id=call.id)
    except IntegrationError as exc:
        call.status = "failed"
        flash(request, f"Call failed: {exc}", "error")
        return RedirectResponse(f"/prospects/{pid}", status_code=303)
    call.provider_call_id = res.provider_call_id
    crm.add_activity(db, p, "call", f"Call started ({prov.name}{', SIMULATED' if res.simulated else ''})",
                     actor_id=user.id, ref_type="call", ref_id=call.id)
    audit(db, "call.start", actor_id=user.id, entity_type="call", entity_id=call.id, data={"provider": prov.name})
    if res.tel_link:
        flash(request, f"Dial {p.phone} from your phone, then log the outcome below (call #{call.id}).")
    else:
        flash(request, "Your phone will ring first; answer to be connected. Log the outcome afterwards.")
    return RedirectResponse(f"/prospects/{pid}#calls", status_code=303)


@router.post("/calls/{cid}/outcome")
def call_outcome(cid: int, request: Request, outcome: str = Form(...), notes: str = Form(""),
                 user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    call = db.get(Call, cid)
    if not call:
        raise HTTPException(404)
    call.outcome, call.notes, call.status = outcome, notes, "logged" if call.status == "initiated" else call.status
    p = db.get(Prospect, call.prospect_id)
    crm.add_activity(db, p, "call", f"Call outcome: {outcome}", body=notes, actor_id=user.id, ref_type="call", ref_id=cid)
    if outcome == "opt_out":
        suppress(db, call.to_number, "call_opt_out", source=f"call #{cid}", actor_id=user.id, kind="phone")
        if p.email:
            suppress(db, p.email, "call_opt_out", source=f"call #{cid}", actor_id=user.id)
        crm.set_stage(db, p, "do_not_contact", actor=user, reason="Asked not to be contacted on a call")
    return RedirectResponse(f"/prospects/{call.prospect_id}#calls", status_code=303)


# --- Import & discovery ------------------------------------------------------------------------

@router.get("/import")
def import_page(request: Request, user: User = Depends(require("rep"))):
    return render(request, "import.html", user, aliases=sorted(set(importer.HEADER_ALIASES.values())))


@router.post("/import")
async def import_upload(request: Request, file: UploadFile = File(...), source_label: str = Form(...),
                        source_url: str = Form(""), commit: str = Form(""), payload: str = Form(""),
                        user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    if commit and payload:
        raw = base64.b64decode(payload)
        filename = request.session.get("import_name", "upload.csv")
    else:
        raw = await file.read()
        filename = file.filename or "upload.csv"
        if len(raw) > 5_000_000:
            raise HTTPException(413, "File too large (5 MB max)")
    try:
        rows = importer.read_rows(filename, raw)
        report = importer.import_rows(db, rows, source_label=source_label, source_url=source_url or None,
                                      dry_run=not commit, actor_id=user.id)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/import", status_code=303)
    request.session["import_name"] = filename
    return render(request, "import.html", user, report=report, source_label=source_label, source_url=source_url,
                  payload=base64.b64encode(raw).decode() if not commit else "",
                  aliases=sorted(set(importer.HEADER_ALIASES.values())))


@router.post("/discover")
def discover(request: Request, source: str = Form(...), query: str = Form(""), user: User = Depends(require("manager")),
             db: Session = Depends(get_db)):
    try:
        res = run_source(db, source, query=query or None, actor_id=user.id)
        flash(request, f"{source}: found {res['found']}, created {res['created']}, matched {res['merged']}"
              + (" [SIMULATED]" if res["simulated"] else ""))
    except IntegrationError as exc:
        flash(request, f"{source} failed: {exc}", "error")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/import", status_code=303)


# --- Tasks -------------------------------------------------------------------------------------

@router.get("/tasks")
def tasks_page(request: Request, show: str = "open", mine: str = "", user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    q = select(Task).order_by(Task.due_at)
    if show == "open":
        q = q.where(Task.status == "open")
    if mine:
        q = q.where(Task.assignee_id == user.id)
    return render(request, "tasks.html", user, tasks=list(db.scalars(q.limit(500))), now=utcnow(), show=show, mine=mine)


@router.post("/tasks/{tid}/done")
def task_done(tid: int, request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    t = db.get(Task, tid)
    if not t:
        raise HTTPException(404)
    crm.complete_task(db, t, actor=user)
    return _back(request, "/tasks")


@router.post("/tasks/{tid}/snooze")
def task_snooze(tid: int, request: Request, days: int = Form(1), user: User = Depends(require("rep")),
                db: Session = Depends(get_db)):
    t = db.get(Task, tid)
    if not t:
        raise HTTPException(404)
    t.due_at = max(t.due_at, utcnow()) + timedelta(days=max(1, min(days, 90)))
    return _back(request, "/tasks")


# --- Meetings ----------------------------------------------------------------------------------

@router.get("/meetings")
def meetings_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Meeting).order_by(Meeting.status, Meeting.start_at.desc().nulls_first()).limit(300)))
    return render(request, "meetings.html", user, rows=rows)


@router.get("/meetings/{mid}")
def meeting_detail(mid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    if not m:
        raise HTTPException(404)
    return render(request, "meeting.html", user, m=m, labels=crm.stage_labels(db))


@router.post("/prospects/{pid}/meeting")
def meeting_request(pid: int, request: Request, attendee_email: str = Form(""), user: User = Depends(require("rep")),
                    db: Session = Depends(get_db)):
    p = _prospect(db, pid)
    m = meetings.open_request(db, p, attendee_email=attendee_email or p.email, source=f"manual by {user.email}", actor_id=user.id)
    try:
        crm.set_stage(db, p, "meeting_requested", actor=user, reason="Meeting request logged")
    except crm.StageError:
        pass
    return RedirectResponse(f"/meetings/{m.id}", status_code=303)


@router.post("/meetings/{mid}/availability")
def meeting_availability(mid: int, request: Request, days: int = Form(7), user: User = Depends(require("rep")),
                         db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    if not m:
        raise HTTPException(404)
    try:
        slots = meetings.suggest_slots(db, m, days=max(1, min(days, 21)))
        flash(request, f"{len(slots)} free slot(s) from {m.availability_source}." if slots else "No free slots found in that range.")
    except IntegrationError as exc:
        m.proposed_slots = None
        flash(request, f"Could not read calendar availability, so no times are suggested: {exc}", "error")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/choose")
def meeting_choose(mid: int, request: Request, start: str = Form(...), user: User = Depends(require("rep")),
                   db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    try:
        meetings.choose_slot(db, m, start, actor=user)
    except meetings.MeetingError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/approve")
def meeting_approve(mid: int, request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    try:
        meetings.approve_booking(db, m, actor=user)
        db.commit()
        # Run the booking now so the person sees the provider result; the worker retries if it fails.
        job = db.scalar(select(Job).where(Job.key == f"book_meeting:{m.id}:{m.start_at.isoformat()}"))
        status = run_job(job.id) if job else "missing"
        db.expire_all()
        m = db.get(Meeting, mid)
        if m.status == "booked":
            flash(request, "Booked. Zoom and calendar confirmed" + (" (SIMULATED)." if m.simulated else "."))
        else:
            flash(request, f"Booking not confirmed (job {status}): {m.error or 'will retry'}", "error")
    except meetings.MeetingError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/reschedule")
def meeting_reschedule(mid: int, request: Request, start: str = Form(...), user: User = Depends(require("manager")),
                       db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    try:
        meetings.reschedule(db, m, start, actor=user)
    except (meetings.MeetingError, IntegrationError) as exc:
        flash(request, f"Not rescheduled: {exc}", "error")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/cancel")
def meeting_cancel(mid: int, request: Request, reason: str = Form(""), user: User = Depends(require("rep")),
                   db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    try:
        meetings.cancel(db, m, actor=user, reason=reason)
    except IntegrationError as exc:
        flash(request, f"Provider error while cancelling (meeting left as is): {exc}", "error")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/complete")
def meeting_complete(mid: int, request: Request, outcome: str = Form("completed"), notes: str = Form(""),
                     user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    try:
        meetings.complete(db, m, actor=user, outcome=outcome, notes=notes)
    except meetings.MeetingError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/prep")
def meeting_prep(mid: int, request: Request, prep_notes: str = Form(""), user: User = Depends(require("rep")),
                 db: Session = Depends(get_db)):
    m = db.get(Meeting, mid)
    m.prep_notes = prep_notes
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


# --- Reports -----------------------------------------------------------------------------------

@router.get("/reports/weekly")
def weekly_report(request: Request, simulated: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "weekly.html", user, r=reports.weekly(db, include_simulated=bool(simulated)))


@router.get("/activity")
def activity_feed(request: Request, kind: str = "", page: int = 1, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    q = select(Activity).order_by(Activity.created_at.desc())
    if kind:
        q = q.where(Activity.kind == kind)
    rows = list(db.scalars(q.offset((max(1, page) - 1) * 100).limit(100)))
    return render(request, "activity.html", user, rows=rows, kind=kind, page=page)

