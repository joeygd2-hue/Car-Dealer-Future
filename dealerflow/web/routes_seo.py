"""SEO audits and monthly client reports (staff)."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db, utcnow
from ..models import Client, ClientReport, SearchMetric, SeoAudit, SeoIssue, SeoSite, User
from ..services import client_reports, seo_audit
from .deps import check_csrf, flash, render, require
from .forms import parse_date

router = APIRouter(dependencies=[Depends(check_csrf)])
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@router.get("/seo")
def seo_page(request: Request, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    sites = db.scalars(select(SeoSite).order_by(SeoSite.id)).all()
    latest = {s.id: db.scalar(select(SeoAudit).where(SeoAudit.site_id == s.id).order_by(SeoAudit.id.desc()).limit(1)) for s in sites}
    return render(request, "seo.html", user, sites=sites, latest=latest, names={c.id: c.name for c in db.scalars(select(Client).order_by(Client.name))})


@router.post("/seo/sites")
def site_create(request: Request, client_id: int = Form(...), base_url: str = Form(...), max_pages: int = Form(50),
                user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    url = base_url.strip()
    if not url.startswith(("http://", "https://")) or db.get(Client, client_id) is None:
        flash(request, "Choose a client and enter a URL starting with http:// or https://", "error")
    elif db.scalar(select(SeoSite.id).where(SeoSite.client_id == client_id, SeoSite.base_url == url)):
        flash(request, "That site is already registered.", "warn")
    else:
        db.add(SeoSite(client_id=client_id, base_url=url[:500], max_pages=max(1, min(max_pages, 500))))
        flash(request, "Site registered. A manager must record the dealership's authorization before it can be audited.")
    return RedirectResponse("/seo", status_code=303)


@router.post("/seo/sites/{sid}/authorize")
def site_authorize(sid: int, request: Request, note: str = Form(...), user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    site = db.get(SeoSite, sid)
    if site is None:
        raise HTTPException(404, "Site not found")
    try:
        seo_audit.authorize_site(db, site, user, note)
        flash(request, "Authorization recorded.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/seo", status_code=303)


@router.post("/seo/sites/{sid}/audit")
def site_audit(sid: int, request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    site = db.get(SeoSite, sid)
    if site is None:
        raise HTTPException(404, "Site not found")
    try:
        a = seo_audit.start_audit(db, site, user)
        flash(request, f"Audit #{a.id} queued; the worker runs it politely (robots.txt, delays, page cap).")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/seo", status_code=303)


@router.get("/seo/clients/{cid}")
def issues_page(cid: int, request: Request, status: str = "", user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    client = db.get(Client, cid)
    if client is None:
        raise HTTPException(404, "Client not found")
    q = select(SeoIssue).where(SeoIssue.client_id == cid)
    if status:
        q = q.where(SeoIssue.status == status)
    issues = sorted(db.scalars(q), key=lambda i: (i.status not in ("open", "in_progress"), SEVERITY_ORDER.get(i.severity, 9)))
    return render(request, "seo_issues.html", user, client=client, issues=issues, status=status)


@router.post("/seo/issues/{iid}")
def issue_status(iid: int, request: Request, status: str = Form(...), user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    i = db.get(SeoIssue, iid)
    if i is None:
        raise HTTPException(404, "Issue not found")
    try:
        seo_audit.update_issue_status(db, i, status, user)
        flash(request, "Issue updated.")
    except ValueError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/seo/clients/{i.client_id}", status_code=303)


def import_search_csv(db: Session, client_id: int, content: str, source: str) -> dict:
    """Search Console performance export: columns date, clicks, impressions, position[, page]."""
    created, errors = 0, []
    for n, row in enumerate(csv.DictReader(io.StringIO(content)), start=2):
        try:
            d = parse_date(row["date"])
            page = (row.get("page") or "").strip()[:1000]
            if db.scalar(select(SearchMetric.id).where(SearchMetric.client_id == client_id, SearchMetric.metric_date == d,
                                                       SearchMetric.source == source, SearchMetric.page == page)):
                continue
            db.add(SearchMetric(client_id=client_id, metric_date=d, source=source, page=page, clicks=int(row.get("clicks") or 0),
                                impressions=int(row.get("impressions") or 0),
                                avg_position=float(row["position"]) if row.get("position") else None))
            created += 1
        except (KeyError, ValueError) as exc:
            errors.append(f"line {n}: {exc}")
    db.flush()
    return {"created": created, "errors": errors}


@router.post("/seo/clients/{cid}/search-metrics")
async def search_import(cid: int, request: Request, file: UploadFile = File(...), user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    if db.get(Client, cid) is None:
        raise HTTPException(404, "Client not found")
    res = import_search_csv(db, cid, (await file.read()).decode("utf-8", "replace")[:5_000_000], "search_console_export")
    flash(request, f"Imported {res['created']} days of Search Console data; {len(res['errors'])} errors.", "warn" if res["errors"] else "info")
    return RedirectResponse(f"/seo/clients/{cid}", status_code=303)


@router.get("/client-reports")
def reports_page(request: Request, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    clients = db.scalars(select(Client).order_by(Client.name)).all()
    ps, pe = client_reports.previous_month(utcnow().date())
    return render(request, "client_reports.html", user, reports=db.scalars(select(ClientReport).order_by(ClientReport.id.desc()).limit(200)).all(),
                  clients=clients, names={c.id: c.name for c in clients}, ps=ps, pe=pe)


@router.post("/client-reports")
def report_create(request: Request, client_id: int = Form(...), period_start: str = Form(...), period_end: str = Form(...),
                  user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    c = db.get(Client, client_id)
    try:
        if c is None:
            raise ValueError("Unknown client")
        r = client_reports.generate_report(db, c, parse_date(period_start), parse_date(period_end), user)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/client-reports", status_code=303)
    flash(request, "Draft generated. Review it, then publish it to the client portal or email it.")
    return RedirectResponse(f"/client-reports/{r.id}", status_code=303)


def _report(db: Session, rid: int) -> ClientReport:
    r = db.get(ClientReport, rid)
    if r is None:
        raise HTTPException(404, "Report not found")
    return r


@router.get("/client-reports/{rid}")
def report_view(rid: int, request: Request, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    r = _report(db, rid)
    return render(request, "client_report.html", user, report=r, d=r.data, staff=True, client=db.get(Client, r.client_id))


@router.get("/client-reports/{rid}/download")
def report_download(rid: int, request: Request, user: User = Depends(require("viewer")), db: Session = Depends(get_db)):
    r = _report(db, rid)
    html = render(request, "client_report.html", user, report=r, d=r.data, staff=False, client=db.get(Client, r.client_id)).body
    return Response(html, media_type="text/html", headers={"Content-Disposition": f'attachment; filename="report-{r.client_id}-{r.period_start}.html"'})


@router.post("/client-reports/{rid}/publish")
def report_publish(rid: int, request: Request, user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    r = _report(db, rid)
    if r.status == "draft":
        r.status = "final"
    flash(request, "Published to the client portal.")
    return RedirectResponse(f"/client-reports/{rid}", status_code=303)


@router.post("/client-reports/{rid}/approve")
def report_approve(rid: int, request: Request, user: User = Depends(require("manager")), db: Session = Depends(get_db)):
    client_reports.approve_report(db, _report(db, rid), user)
    flash(request, "Approved. This version of the report can now be emailed.")
    return RedirectResponse(f"/client-reports/{rid}", status_code=303)


@router.post("/client-reports/{rid}/send")
def report_send(rid: int, request: Request, to: str = Form(...), user: User = Depends(require("rep")), db: Session = Depends(get_db)):
    r = _report(db, rid)
    if "@" not in to or len(to) > 255:
        flash(request, "Enter a valid recipient.", "error")
        return RedirectResponse(f"/client-reports/{rid}", status_code=303)
    outcome = client_reports.send_report(db, r, to.strip(), user)
    flash(request, {"sent": "Emailed through the authorized provider.",
                    "simulated": "SIMULATED: recorded in the mock outbox; nothing was emailed."}.get(outcome, f"Not emailed ({outcome})."),
          "info" if outcome == "sent" else "warn")
    return RedirectResponse(f"/client-reports/{rid}", status_code=303)
