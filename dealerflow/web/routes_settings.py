"""Settings: templates, scoring, knowledge base, campaigns, users, costs, integrations, audit log."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..db import get_db, utcnow
from ..models import STAFF_ROLES, AuditLog, Campaign, CostEntry, IntegrationLog, KnowledgeItem, PipelineStage, Template, User
from ..security import hash_password, new_api_key
from ..services import reports
from ..services.outreach import ALLOWED_VARS, validate_template
from ..services.settings_store import get_setting, put_setting
from .deps import check_csrf, current_user, flash, render, require

router = APIRouter(dependencies=[Depends(check_csrf)])


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "settings.html", user,
                  templates_=list(db.scalars(select(Template).order_by(Template.name, Template.language))),
                  weights=get_setting(db, "scoring_weights"), threshold=get_setting(db, "qualification_threshold"),
                  cost_estimates=get_setting(db, "cost_estimates"),
                  knowledge=list(db.scalars(select(KnowledgeItem).order_by(KnowledgeItem.topic))),
                  campaigns=list(db.scalars(select(Campaign).order_by(Campaign.name))),
                  stages=list(db.scalars(select(PipelineStage).order_by(PipelineStage.position))),
                  users=list(db.scalars(select(User).order_by(User.email))), roles=STAFF_ROLES, allowed_vars=ALLOWED_VARS,
                  costs=list(db.scalars(select(CostEntry).order_by(CostEntry.incurred_on.desc()).limit(50))))


@router.post("/settings/template")
def template_save(request: Request, template_id: str = Form(""), name: str = Form(...), language: str = Form("en"),
                  subject: str = Form(...), body: str = Form(...), active: str = Form(""),
                  user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    errs = validate_template(subject, body)
    if language not in ("en", "es"):
        errs.append("language must be en or es")
    if errs:
        flash(request, "; ".join(errs), "error")
        return RedirectResponse("/settings#templates", status_code=303)
    tpl = db.get(Template, int(template_id)) if template_id else Template()
    tpl.name, tpl.language, tpl.subject, tpl.body, tpl.active = name.strip(), language, subject.strip(), body, bool(active)
    db.add(tpl)
    db.flush()
    audit(db, "template.save", actor_id=user.id, entity_type="template", entity_id=tpl.id)
    flash(request, "Template saved.")
    return RedirectResponse("/settings#templates", status_code=303)


@router.post("/settings/scoring")
async def scoring_save(request: Request, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    form = await request.form()
    weights = get_setting(db, "scoring_weights")
    try:
        new = {k: float(form.get(f"w_{k}", v)) for k, v in weights.items()}
        if any(v < 0 for v in new.values()):
            raise ValueError("weights cannot be negative")
        threshold = float(form.get("threshold", 55))
    except ValueError as exc:
        flash(request, f"Not saved: {exc}", "error")
        return RedirectResponse("/settings#scoring", status_code=303)
    put_setting(db, "scoring_weights", new, actor_id=user.id)
    put_setting(db, "qualification_threshold", threshold, actor_id=user.id)
    if form.get("cost_estimates"):
        try:
            put_setting(db, "cost_estimates", {k: float(v) for k, v in json.loads(str(form["cost_estimates"])).items()},
                        actor_id=user.id)
        except (ValueError, AttributeError) as exc:
            flash(request, f"Cost estimates not saved: {exc}", "error")
            return RedirectResponse("/settings#scoring", status_code=303)
    flash(request, "Scoring saved. Prospects are re-scored on the next agent tick or with 'Re-score'.")
    return RedirectResponse("/settings#scoring", status_code=303)


@router.post("/settings/knowledge")
def knowledge_save(request: Request, item_id: str = Form(""), topic: str = Form(...), keywords: str = Form(...),
                   answer_en: str = Form(""), answer_es: str = Form(""), approved: str = Form(""),
                   user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    it = db.get(KnowledgeItem, int(item_id)) if item_id else KnowledgeItem()
    changed = (it.answer_en, it.answer_es) != (answer_en, answer_es)
    it.topic, it.keywords, it.answer_en, it.answer_es = topic, keywords, answer_en, answer_es
    want_approved = bool(approved)
    if want_approved and ("[EDIT" in answer_en or "[EDITAR" in answer_es):
        flash(request, "Replace the [EDIT…] placeholder text before approving.", "error")
        want_approved = False
    if want_approved and user.role != "admin":
        flash(request, "Only an admin can approve answers the agent may send.", "error")
        want_approved = False
    it.approved = want_approved
    it.approved_by_id = user.id if want_approved else None
    it.approved_at = utcnow() if want_approved else None
    db.add(it)
    db.flush()
    audit(db, "knowledge.save", actor_id=user.id, entity_type="knowledge", entity_id=it.id, data={"approved": it.approved, "answer_changed": changed})
    return RedirectResponse("/settings#knowledge", status_code=303)


@router.post("/settings/campaign")
def campaign_save(request: Request, campaign_id: str = Form(""), name: str = Form(...), steps: str = Form(...),
                  min_score: float = Form(55), daily_cap: int = Form(10), language_mode: str = Form("auto"),
                  target_cities: str = Form(""), user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    try:
        parsed = json.loads(steps)
        assert isinstance(parsed, list) and parsed and all("template" in s for s in parsed)
    except (ValueError, AssertionError):
        flash(request, 'Steps must be JSON like [{"template": "intro", "delay_days": 0}, ...]', "error")
        return RedirectResponse("/settings#campaigns", status_code=303)
    for s in parsed:
        if not db.scalar(select(Template).where(Template.name == s["template"])):
            flash(request, f"No template named {s['template']!r}", "error")
            return RedirectResponse("/settings#campaigns", status_code=303)
    c = db.get(Campaign, int(campaign_id)) if campaign_id else Campaign(status="paused", approval_mode="manual")
    c.name, c.steps, c.min_score, c.daily_cap = name, parsed, min_score, max(0, daily_cap)
    c.language_mode, c.target_cities = language_mode, target_cities
    db.add(c)
    db.flush()
    audit(db, "campaign.save", actor_id=user.id, entity_type="campaign", entity_id=c.id)
    return RedirectResponse("/settings#campaigns", status_code=303)


@router.post("/settings/stage")
def stage_save(request: Request, key: str = Form(...), label: str = Form(...), position: int = Form(...),
               active: str = Form(""), user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    st = db.get(PipelineStage, key)
    if not st:
        raise HTTPException(404)
    st.label, st.position, st.active = label, position, bool(active) or key in ("new", "do_not_contact")
    audit(db, "stage.save", actor_id=user.id, data={"key": key, "label": label, "position": position, "active": st.active})
    return RedirectResponse("/settings#stages", status_code=303)


@router.post("/settings/user")
def user_save(request: Request, email: str = Form(...), name: str = Form(""), role: str = Form("rep"),
              password: str = Form(""), active: str = Form("1"), user: User = Depends(require("admin")),
              db: Session = Depends(get_db)):
    if role not in STAFF_ROLES:  # client users are created from the client's page, bound to that client
        raise HTTPException(400, "bad role")
    u = db.scalar(select(User).where(User.email == email.lower().strip()))
    try:
        if u is None:
            u = User(email=email.lower().strip(), name=name, role=role, password_hash=hash_password(password))
            db.add(u)
        else:
            if u.is_client:
                raise ValueError("That address belongs to a client portal user; manage it from the client's page")
            if u.id == user.id and (role != "admin" or active in ("", "0")):
                raise ValueError("You cannot demote or deactivate yourself")
            u.name, u.role, u.active = name or u.name, role, active not in ("", "0")
            if password:
                u.password_hash = hash_password(password)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings#users", status_code=303)
    db.flush()
    if not u.active or password:  # deactivation or an admin-set password ends that user's sessions everywhere
        from .. import sessions

        sessions.end_all(db, u.id)
    audit(db, "user.save", actor_id=user.id, entity_type="user", entity_id=u.id, data={"role": role, "active": u.active})
    return RedirectResponse("/settings#users", status_code=303)


@router.post("/settings/users/{uid}/reset-link")
def user_reset_link(uid: int, request: Request, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    """Single-use setup/reset link for a user (staff or dealership), shown once to the admin to hand over."""
    from .routes_auth import issue_reset_token

    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "Not found")
    link = issue_reset_token(db, target, created_by_id=user.id, purpose="SETUP")
    db.commit()
    return render(request, "reset_link.html", user, target=target, link=link)


@router.post("/settings/api-key")
def api_key(request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    key, digest = new_api_key()
    user.api_key_hash = digest
    audit(db, "user.api_key", actor_id=user.id, entity_type="user", entity_id=user.id)
    flash(request, f"New API key (shown once): {key}", "warn")
    return RedirectResponse("/settings#users", status_code=303)


@router.post("/settings/cost")
def cost_add(request: Request, incurred_on: str = Form(...), category: str = Form(...), amount: float = Form(...),
             lead_source: str = Form(""), note: str = Form(""), user: User = Depends(require("manager")),
             db: Session = Depends(get_db)):
    db.add(CostEntry(incurred_on=datetime.fromisoformat(incurred_on), category=category, amount=amount,
                     lead_source=lead_source or None, note=note, is_estimate=False))
    audit(db, "cost.add", actor_id=user.id, data={"category": category, "amount": amount})
    return RedirectResponse("/settings#costs", status_code=303)


@router.get("/integrations")
def integrations_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    logs = list(db.scalars(select(IntegrationLog).order_by(IntegrationLog.id.desc()).limit(200)))
    return render(request, "integrations.html", user, health=reports.integration_health(db), logs=logs, s=get_settings())


@router.post("/integrations/check")
def integrations_check(request: Request, user: User = Depends(require("admin")), db: Session = Depends(get_db)):
    from ..audit import log_integration, set_integration_disabled
    from ..services.agent import state as get_agent_state
    from ..integrations.calendar import get_calendar_provider
    from ..integrations.email import get_email_provider
    from ..integrations.zoom import get_zoom_provider

    results = []
    for label, factory in (("email", get_email_provider), ("calendar", get_calendar_provider), ("zoom", get_zoom_provider)):
        try:
            prov = factory()
            if get_agent_state(db).status == "emergency_stopped":
                raise RuntimeError("emergency stop is active; checks make external calls")
            detail = prov.check()
            log_integration(db, prov.name, "check", "ok", detail=detail, simulated=prov.simulated)
            set_integration_disabled(db, prov.name, None)  # a passing check re-enables a disabled provider
            results.append(f"{label}: {detail}")
        except Exception as exc:  # noqa: BLE001
            log_integration(db, label, "check", "auth_error" if "auth" in str(exc).lower() or "configured" in str(exc).lower() else "error",
                            detail=str(exc))
            results.append(f"{label}: FAILED — {exc}")
    audit(db, "integration.check", actor_id=user.id, data={"results": results})
    flash(request, " | ".join(results), "info")
    return RedirectResponse("/integrations", status_code=303)


@router.get("/audit")
def audit_page(request: Request, action: str = "", page: int = 1, user: User = Depends(require("manager")),
               db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        q = q.where(AuditLog.action.startswith(action))
    rows = list(db.scalars(q.offset((max(1, page) - 1) * 100).limit(100)))
    return render(request, "audit.html", user, rows=rows, action=action, page=page)
