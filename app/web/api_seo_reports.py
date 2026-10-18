"""SEO and client-report API (staff)."""
from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.seo import ClientReport, SearchMetric, SeoAudit, SeoIssue, SeoSite
from app.security.auth import AuthContext, require_admin, require_staff
from app.services import reports
from app.services.seo import audit as seo
from app.web.api_core import bad, client_or_404

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_staff)])


class SiteIn(BaseModel):
    client_id: int
    base_url: str = Field(pattern=r"^https?://[^\s/]+", max_length=300)
    max_pages: int = Field(50, ge=1, le=500)
    crawl_delay_seconds: float = Field(1.0, ge=0.5, le=30)


@router.post("/seo/sites", status_code=201)
def create_site(body: SiteIn, db: Session = Depends(get_db)):
    client_or_404(db, body.client_id)
    site = SeoSite(**body.model_dump())
    db.add(site)
    db.commit()
    return {"id": site.id, "authorized": site.authorized}


class AuthorizeIn(BaseModel):
    note: str = Field(min_length=5)


@router.post("/seo/sites/{site_id}/authorize")
def authorize(site_id: int, body: AuthorizeIn, ctx: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    site = db.get(SeoSite, site_id)
    if site is None:
        raise HTTPException(404, "Not found")
    try:
        seo.authorize_site(db, site, ctx.user, body.note)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": site.id, "authorized": True}


@router.post("/seo/sites/{site_id}/audits", status_code=202)
def start(site_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    site = db.get(SeoSite, site_id)
    if site is None:
        raise HTTPException(404, "Not found")
    try:
        a = seo.start_audit(db, site, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"audit_id": a.id, "status": a.status}


@router.get("/seo/audits/{audit_id}")
def get_audit(audit_id: int, db: Session = Depends(get_db)):
    a = db.get(SeoAudit, audit_id)
    if a is None:
        raise HTTPException(404, "Not found")
    return {"id": a.id, "status": a.status, "pages_crawled": a.pages_crawled, "summary": a.summary, "error": a.error, "started_at": a.started_at, "finished_at": a.finished_at}


def issue_out(i: SeoIssue) -> dict:
    return {k: getattr(i, k) for k in ("id", "code", "finding", "evidence", "url", "severity", "impact", "recommended_fix", "effort", "responsible_party",
                                         "status", "detection", "before_value", "after_value", "first_seen_audit_id", "last_seen_audit_id", "resolved_at")}


@router.get("/seo/issues")
def list_issues(client_id: int, status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(SeoIssue).where(SeoIssue.client_id == client_id).order_by(SeoIssue.severity, SeoIssue.id)
    if status:
        stmt = stmt.where(SeoIssue.status == status)
    return [issue_out(i) for i in db.scalars(stmt)]


class IssueStatusIn(BaseModel):
    status: str


@router.patch("/seo/issues/{issue_id}")
def patch_issue(issue_id: int, body: IssueStatusIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    i = db.get(SeoIssue, issue_id)
    if i is None:
        raise HTTPException(404, "Not found")
    try:
        seo.update_issue_status(db, i, body.status, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return issue_out(i)


class SearchImportIn(BaseModel):
    client_id: int
    source: str = Field("search_console_export", max_length=40)
    csv: str = Field(max_length=5_000_000)


@router.post("/seo/search-metrics/import")
def import_search_metrics(body: SearchImportIn, db: Session = Depends(get_db)):
    """Import a Search Console performance export (columns: date, clicks, impressions, position[, page])."""
    client_or_404(db, body.client_id)
    created, errors = 0, []
    for i, row in enumerate(csv.DictReader(io.StringIO(body.csv)), start=2):
        try:
            d = date.fromisoformat(row["date"].strip())
            page = (row.get("page") or "").strip() or None
            if db.scalar(select(SearchMetric.id).where(SearchMetric.client_id == body.client_id, SearchMetric.metric_date == d, SearchMetric.source == body.source, SearchMetric.page == page)):
                continue
            db.add(SearchMetric(client_id=body.client_id, metric_date=d, source=body.source, page=page, clicks=int(row.get("clicks") or 0),
                                impressions=int(row.get("impressions") or 0), avg_position=float(row["position"]) if row.get("position") else None))
            created += 1
        except (KeyError, ValueError) as exc:
            errors.append(f"line {i}: {exc}")
    db.commit()
    return {"created": created, "errors": errors}


class ReportIn(BaseModel):
    client_id: int
    period_start: date
    period_end: date


@router.post("/reports", status_code=201)
def create_report(body: ReportIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    c = client_or_404(db, body.client_id)
    try:
        r = reports.generate_report(db, c, body.period_start, body.period_end, ctx.user)
    except ValueError as exc:
        raise bad(exc) from exc
    db.commit()
    return {"id": r.id, "data": r.data}


@router.get("/reports/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db)):
    r = db.get(ClientReport, report_id)
    if r is None:
        raise HTTPException(404, "Not found")
    return {"id": r.id, "client_id": r.client_id, "status": r.status, "data": r.data, "sent_at": r.sent_at}


@router.post("/reports/{report_id}/finalize")
def finalize(report_id: int, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    r = db.get(ClientReport, report_id)
    if r is None:
        raise HTTPException(404, "Not found")
    if r.status == "draft":
        r.status = "final"
    db.commit()
    return {"id": r.id, "status": r.status}


class SendIn(BaseModel):
    to: EmailStr


@router.post("/reports/{report_id}/send")
def send(report_id: int, body: SendIn, ctx: AuthContext = Depends(require_staff), db: Session = Depends(get_db)):
    r = db.get(ClientReport, report_id)
    if r is None:
        raise HTTPException(404, "Not found")
    status = reports.send_report(db, r, body.to, ctx.user)
    db.commit()
    return {"delivery": status, "note": "Sandboxed: no authorized report-email integration is connected for this client" if status == "sandboxed" else None}
