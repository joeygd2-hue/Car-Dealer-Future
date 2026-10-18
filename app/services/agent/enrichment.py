"""Enrich and qualify prospects using only evidence we observed.

Every stored observation has a source URL and observation time. Emails are
only taken from the prospect's own website (mailto: links or visible text on
pages robots.txt allows). Qualification is rule-based and explainable.
"""
from __future__ import annotations

import re

import httpx
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.outreach import Prospect
from app.services.agent.discovery import FRANCHISE_HINTS, domain_of
from app.services.seo.checks import run_checks
from app.services.seo.crawler import Crawler

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ROLE_PREFERENCE = ("sales", "info", "contact", "office", "manager", "internet")
BAD_EMAIL_HINTS = ("example.", "sentry", "wixpress", ".png", ".jpg", "noreply", "no-reply", "@domain")

# Measured observations we may mention in outreach, mapped from SEO finding codes.
OBSERVATION_TEXT = {
    "spanish_content_opportunity": "we didn't find Spanish-language pages on {domain}",
    "inventory_schema_missing": "vehicle pages on {domain} don't include vehicle structured data",
    "meta_description_missing": "some pages on {domain} have no meta description",
    "img_alt_missing": "some vehicle photos on {domain} have no alt text",
    "sitemap_missing": "we couldn't find an XML sitemap on {domain}",
}

DEFAULT_TARGET_CITIES = {"sugar land", "stafford", "missouri city", "richmond", "rosenberg", "houston", "katy", "pearland", "meadows place", "fresno"}


def enrich(db: Session, p: Prospect, http_client: httpx.Client | None = None, sleep=None, max_pages: int = 5) -> Prospect:
    p.research_attempts += 1
    p.stage = "researching"
    evidence: list[dict] = list(p.evidence or [])
    now = utcnow().isoformat(timespec="seconds")
    if p.website:
        kwargs = {"max_pages": max_pages, "delay_seconds": 1.0, "client": http_client}
        if sleep is not None:
            kwargs["sleep"] = sleep
        crawler = Crawler(p.website, **kwargs)
        crawl = crawler.crawl(seed_urls=[crawler.base + "contact", crawler.base + "contact-us"], check_links_limit=0)
        ok_pages = [pg for pg in crawl.pages if pg.status_code == 200 and pg.html]
        if ok_pages:
            evidence.append({"fact": "Website reachable", "source_url": ok_pages[0].url, "observed_at": now, "method": "measured"})
        if not p.email:
            email, src = _find_email(ok_pages, domain_of(p.website))
            if email:
                p.email, p.email_source_url = email, src
                evidence.append({"fact": f"Public contact email {email}", "source_url": src, "observed_at": now, "method": "measured"})
        findings, _ = run_checks(crawl, crawler.base, {"name": p.business_name, "phone": p.phone})
        d = domain_of(p.website)
        for f in findings:
            if f.code in OBSERVATION_TEXT and f.detection in ("measured", "heuristic"):
                evidence.append({
                    "fact": OBSERVATION_TEXT[f.code].format(domain=d), "source_url": f.url, "observed_at": now,
                    "method": f.detection, "code": f.code,
                })
    # de-duplicate evidence by fact
    seen, uniq = set(), []
    for e in evidence:
        if e["fact"] not in seen:
            seen.add(e["fact"])
            uniq.append(e)
    p.evidence = uniq
    p.enriched_at = utcnow()
    qualify(p)
    return p


def _find_email(pages, domain: str | None) -> tuple[str | None, str | None]:
    from bs4 import BeautifulSoup

    candidates: list[tuple[str, str]] = []
    for pg in pages:
        soup = BeautifulSoup(pg.html, "html.parser")
        for a in soup.find_all("a", href=True):
            if a["href"].lower().startswith("mailto:"):
                addr = a["href"][7:].split("?")[0].strip().lower()
                candidates.append((addr, pg.url))
        for m in EMAIL_RE.findall(soup.get_text(" ")):
            candidates.append((m.lower(), pg.url))
    good = [(e, u) for e, u in candidates if EMAIL_RE.fullmatch(e) and not any(b in e for b in BAD_EMAIL_HINTS)]
    if domain:
        same = [(e, u) for e, u in good if e.endswith("@" + domain) or e.endswith("." + domain)]
        good = same or good
    for pref in ROLE_PREFERENCE:
        for e, u in good:
            if e.startswith(pref):
                return e, u
    return good[0] if good else (None, None)


def qualify(p: Prospect, target_cities: set[str] | None = None) -> None:
    reasons: list[str] = []
    cities = target_cities or DEFAULT_TARGET_CITIES
    if FRANCHISE_HINTS.search(p.business_name or ""):
        p.is_independent = False
    if p.is_independent is False:
        p.stage, p.disqualify_reason = "disqualified", "Appears to be a franchise/brand dealership (name match)"
        return
    if p.city and p.city.strip().lower() not in cities:
        p.stage, p.disqualify_reason = "disqualified", f"Outside target area ({p.city})"
        return
    if not p.email:
        p.stage, p.disqualify_reason = "disqualified", "No publicly listed email address found"
        return
    score = 50.0
    if p.website:
        score += 10
        reasons.append("has website")
    obs = [e for e in p.evidence if e.get("code")]
    score += min(30, 10 * len(obs))
    if obs:
        reasons.append(f"{len(obs)} evidenced improvement opportunities")
    if p.city and p.city.lower() == "sugar land":
        score += 10
        reasons.append("in Sugar Land")
    p.priority = score
    p.qualification_notes = "; ".join(reasons) or "meets minimum criteria"
    p.stage, p.qualified_at = "qualified", utcnow()
