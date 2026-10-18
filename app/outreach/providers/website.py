"""Evidence-based enrichment from a prospect's own public website.

Polite and limited: honours robots.txt, identifies itself, fetches at most a
few pages (home + contact/about), no logins, no CAPTCHA/anti-bot evasion, no
form submission. Every observation stores the URL and the exact excerpt.
"""
from __future__ import annotations

import html
import re
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from app.outreach.providers.sandbox import sandbox_site_html

USER_AGENT = "CarDealerFutureBot/0.2 (+business research; contact via site owner)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_transport: httpx.BaseTransport | None = None


def set_transport(t):
    global _transport
    _transport = t


@dataclass
class Finding:
    kind: str
    statement: str
    url: str
    excerpt: str


@dataclass
class ContactFinding:
    email: str
    url: str
    excerpt: str


def _text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))).strip()


def _excerpt(text: str, needle: str, width: int = 80) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return needle
    return text[max(0, i - width // 2): i + len(needle) + width // 2].strip()


def fetch_pages(website: str, max_pages: int = 3) -> dict[str, str]:
    u = urlparse(website if "://" in website else f"https://{website}")
    domain = u.netloc.lower()
    fixture = sandbox_site_html(domain)
    if fixture is not None:
        return {urljoin(f"https://{domain}", p): body for p, body in fixture.items()}
    base = f"{u.scheme or 'https'}://{domain}"
    pages: dict[str, str] = {}
    with httpx.Client(timeout=10, transport=_transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = client.get(f"{base}/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        for path in ["/", "/contact", "/contact-us", "/about", "/about-us"]:
            if len(pages) >= max_pages:
                break
            url = base + path
            if not rp.can_fetch(USER_AGENT, url):
                continue
            try:
                resp = client.get(url)
            except httpx.HTTPError:
                continue
            if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "text/html"):
                pages[url] = resp.text[:500_000]
    return pages


def analyze(website: str, pages: dict[str, str]) -> tuple[list[Finding], list[ContactFinding]]:
    domain = urlparse(website if "://" in website else f"https://{website}").netloc.lower().removeprefix("www.")
    findings: list[Finding] = []
    contacts: list[ContactFinding] = []
    seen_emails = set()
    for url, page in pages.items():
        text = _text(page)
        title = re.search(r"(?is)<title>(.*?)</title>", page)
        if title and url.rstrip("/").endswith(domain):
            t = html.unescape(title.group(1)).strip()
            findings.append(Finding("website_title", f"Website title: \"{t[:120]}\"", url, t[:200]))
        if re.search(r'href="[^"]*(inventory|vehicles|cars-for-sale|used-cars)', page, re.I):
            findings.append(Finding("has_inventory_page", "Website links to an online inventory page", url,
                                    _excerpt(text, "inventory")))
        if re.search(r"se habla espa[nñ]ol|hablamos espa[nñ]ol", text, re.I):
            findings.append(Finding("spanish_service", "Website states Spanish-language service", url,
                                    _excerpt(text, "habla")))
        if re.search(r"facebook\.com/", page, re.I):
            findings.append(Finding("facebook_link", "Website links to a Facebook page", url, "facebook.com link on page"))
        for email in set(EMAIL_RE.findall(page)):
            e = email.lower().strip(".")
            if e in seen_emails or e.endswith((".png", ".jpg", ".gif", ".webp")):
                continue
            if not (e.endswith("@" + domain) or e.split("@")[1] in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com")):
                continue
            seen_emails.add(e)
            contacts.append(ContactFinding(e, url, _excerpt(text, e)))
    uniq, keys = [], set()
    for f in findings:
        if f.kind not in keys:
            keys.add(f.kind)
            uniq.append(f)
    return uniq, contacts
