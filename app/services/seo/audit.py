"""SEO audit orchestration: crawl -> checks -> issue lifecycle (open/resolved with before/after)."""
from __future__ import annotations

import hashlib

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.base import utcnow
from app.models.core import Client, User
from app.models.seo import SeoAudit, SeoIssue, SeoPage, SeoSite
from app.services import audit as audit_log
from app.services import costs
from app.services.jobs import PermanentJobError, handler
from app.services.seo.checks import Finding, run_checks
from app.services.seo.crawler import Crawler

PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


class SeoError(ValueError):
    pass


def fingerprint(code: str, url: str | None) -> str:
    return hashlib.sha256(f"{code}|{url or ''}".encode()).hexdigest()


def authorize_site(db: Session, site: SeoSite, actor: User, note: str) -> None:
    if not note.strip():
        raise SeoError("Record how the dealership authorized the audit (e.g. agreement reference)")
    site.authorized, site.authorization_note = True, note.strip()
    site.authorized_by_user_id, site.authorized_at = actor.id, utcnow()
    audit_log.record(db, "seo.site_authorized", actor=actor, entity=site, details={"note": note})


def start_audit(db: Session, site: SeoSite, actor: User | None) -> SeoAudit:
    if not site.authorized:
        raise SeoError("This site has not been authorized for auditing")
    a = SeoAudit(client_id=site.client_id, site_id=site.id, status="queued", triggered_by_user_id=actor.id if actor else None)
    db.add(a)
    db.flush()
    from app.services.jobs import enqueue

    enqueue(db, "seo.run_audit", {"audit_id": a.id}, idempotency_key=f"seo_audit:{a.id}", client_id=site.client_id, max_attempts=2)
    audit_log.record(db, "seo.audit_started", actor=actor, entity=a)
    return a


def pagespeed(url: str, client: httpx.Client | None = None) -> dict | None:
    key = get_settings().pagespeed_api_key
    if not key:
        return None
    c = client or httpx.Client(timeout=90)
    r = c.get(PAGESPEED_ENDPOINT, params={"url": url, "key": key, "strategy": "mobile", "category": "performance"})
    if r.status_code != 200:
        return {"error": f"PageSpeed API returned {r.status_code}"}
    data = r.json()
    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    return {
        "source": "Google PageSpeed Insights API (Lighthouse lab data, mobile)",
        "performance_score": (lh.get("categories", {}).get("performance", {}) or {}).get("score"),
        "lcp_ms": (audits.get("largest-contentful-paint") or {}).get("numericValue"),
        "cls": (audits.get("cumulative-layout-shift") or {}).get("numericValue"),
        "tbt_ms": (audits.get("total-blocking-time") or {}).get("numericValue"),
        "field_inp_category": ((data.get("loadingExperience") or {}).get("metrics", {}).get("INTERACTION_TO_NEXT_PAINT") or {}).get("category"),
    }


def run_audit(db: Session, audit: SeoAudit, http_client: httpx.Client | None = None, sleep=None, pagespeed_client: httpx.Client | None = None) -> SeoAudit:
    site = db.get(SeoSite, audit.site_id)
    client = db.get(Client, audit.client_id)
    if not site.authorized:
        audit.status, audit.error = "failed", "Site authorization was revoked"
        return audit
    audit.status, audit.started_at = "running", utcnow()
    db.flush()
    kwargs = {"max_pages": site.max_pages, "delay_seconds": site.crawl_delay_seconds, "client": http_client}
    if sleep is not None:
        kwargs["sleep"] = sleep
    crawler = Crawler(site.base_url, **kwargs)
    crawl = crawler.crawl()
    business = {"name": client.name, "phone": client.phone, "address": client.address_line1, "city": client.city}
    findings, parsed = run_checks(crawl, crawler.base, business)

    perf = None
    try:
        perf = pagespeed(site.base_url, pagespeed_client)
        if perf and "error" not in perf:
            costs.record_usage(db, provider="google:pagespeed", service_category="seo", metric="requests", quantity=1, client_id=client.id, feature="seo_audit", idempotency_key=f"psi:{audit.id}")
            score = perf.get("performance_score")
            if score is not None and score < 0.5:
                findings.append(Finding("performance_low", "Mobile performance score is low", f"Lighthouse performance score {round(score * 100)}/100; LCP {perf.get('lcp_ms')} ms", site.base_url, "high" if score < 0.3 else "medium",
                                        "Slow pages can frustrate mobile shoppers.", "Compress vehicle images, defer non-critical scripts, and review third-party widgets.", "medium", "web_provider", current_value=str(round(score * 100))))
    except httpx.HTTPError as exc:
        perf = {"error": f"PageSpeed request failed: {exc}"}

    for p in crawl.pages:
        d = parsed.get(p.url, {})
        db.add(SeoPage(audit_id=audit.id, client_id=client.id, url=p.url[:1000], status_code=p.status_code, data={k: v for k, v in d.items() if k not in ("text_sample", "links", "jsonld")}))

    _apply_findings(db, audit, site, findings, crawled_urls={p.url for p in crawl.pages})
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    audit.pages_crawled = len(crawl.pages)
    audit.pages_skipped_robots = len(crawl.robots_blocked_urls)
    audit.summary = {
        "issues_by_severity": counts,
        "issues_total": len(findings),
        "measured": sum(1 for f in findings if f.detection == "measured"),
        "heuristic": sum(1 for f in findings if f.detection == "heuristic"),
        "robots_txt_found": crawl.robots_txt_found,
        "sitemap_found": crawl.sitemap_found,
        "sitemap_url_count": len(crawl.sitemap_urls),
        "stopped_reason": crawl.stopped_reason,
        "performance": perf,
        "search_console": "not connected (import Search Console exports via the Search metrics upload)",
    }
    audit.status, audit.finished_at = "completed", utcnow()
    return audit


def _apply_findings(db: Session, audit: SeoAudit, site: SeoSite, findings: list[Finding], crawled_urls: set[str]) -> None:
    seen: set[str] = set()
    for f in findings:
        fp = fingerprint(f.code, f.url)
        if fp in seen:
            continue
        seen.add(fp)
        issue = db.scalar(select(SeoIssue).where(SeoIssue.site_id == site.id, SeoIssue.fingerprint == fp))
        if issue is None:
            db.add(SeoIssue(
                client_id=site.client_id, site_id=site.id, fingerprint=fp, code=f.code, finding=f.finding, evidence=f.evidence,
                url=f.url, severity=f.severity, impact=f.impact, recommended_fix=f.recommended_fix, effort=f.effort,
                responsible_party=f.responsible_party, detection=f.detection, status="open", before_value=f.current_value,
                first_seen_audit_id=audit.id, last_seen_audit_id=audit.id,
            ))
        else:
            issue.last_seen_audit_id, issue.evidence = audit.id, f.evidence
            if issue.status == "resolved":
                issue.status, issue.resolved_at, issue.resolved_audit_id = "open", None, None  # regression
    # Issues not seen this time are resolved — but only if we could actually re-check them.
    site_level = {"robots_missing", "sitemap_missing", "spanish_content_opportunity", "local_schema_missing", "title_duplicate", "meta_description_duplicate"}
    for issue in db.scalars(select(SeoIssue).where(SeoIssue.site_id == site.id, SeoIssue.status.in_(("open", "in_progress")))):
        if issue.fingerprint in seen:
            continue
        if issue.code in site_level or (issue.url and issue.url in crawled_urls):
            issue.status, issue.resolved_at, issue.resolved_audit_id = "resolved", utcnow(), audit.id
            issue.after_value = "Not detected in audit #%d" % audit.id


def update_issue_status(db: Session, issue: SeoIssue, status: str, actor: User) -> None:
    if status not in ("open", "in_progress", "resolved", "wont_fix"):
        raise SeoError("Invalid status")
    old = issue.status
    issue.status = status
    if status == "resolved":
        issue.resolved_at = utcnow()
        issue.after_value = issue.after_value or "Marked resolved manually (not yet re-measured)"
    audit_log.record(db, "seo.issue_status", actor=actor, entity=issue, details={"from": old, "to": status})


@handler("seo.run_audit")
def _job_run_audit(db: Session, job) -> dict:
    audit = db.get(SeoAudit, job.payload["audit_id"])
    if audit is None:
        raise PermanentJobError("Audit not found")
    if audit.status == "completed":
        return {"skipped": "already completed"}
    try:
        run_audit(db, audit)
    except Exception as exc:
        audit.status, audit.error = "failed", str(exc)[:2000]
        db.commit()
        raise
    return {"pages": audit.pages_crawled, "issues": audit.summary.get("issues_total")}
