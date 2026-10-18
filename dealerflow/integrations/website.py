"""Dealer website research: fetch a dealer's own public pages, honouring robots.txt.

Only runs when DF_WEBSITE_RESEARCH_ENABLED=true. Requests identify themselves with
DF_HTTP_USER_AGENT, are rate limited, read at most a few pages per site, never log in,
never submit forms and never try to get around blocks or CAPTCHAs. Every observation is
stored as unverified evidence with the URL and time it was seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import get_settings
from .errors import IntegrationError, NotConfiguredError, PermanentError
from .http import request

INVENTORY_HINTS = re.compile(r"(inventory|used-cars|used-vehicles|pre-owned|vehicles|cars-for-sale|inventario)", re.I)
SPANISH_HINTS = re.compile(r"(se habla espa[nñ]ol|hablamos espa[nñ]ol|en espa[nñ]ol|financiamiento|cr[eé]dito f[aá]cil)", re.I)
VEHICLE_CARD = re.compile(r"\b(19[89]\d|20[0-3]\d)\s+(acura|audi|bmw|buick|cadillac|chevrolet|chevy|chrysler|dodge|ford|gmc|honda|hyundai|infiniti|jeep|kia|lexus|lincoln|mazda|mercedes|nissan|ram|subaru|tesla|toyota|volkswagen|volvo)\b", re.I)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.mailtos: list[str] = []
        self.forms = 0
        self.viewport = False
        self.lang = ""
        self.title = ""
        self._in_title = False
        self._href: str | None = None
        self._text: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = (a.get("lang") or "").lower()
        elif tag == "a" and a.get("href"):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?")[0].strip().lower())
            else:
                self._href = href
                self._text = []
        elif tag == "form":
            self.forms += 1
        elif tag == "meta" and (a.get("name") or "").lower() == "viewport":
            self.viewport = True
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and a.get("src"):
            self.scripts.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._href is not None:
            self._text.append(data.strip())


@dataclass
class SiteObservation:
    url: str
    final_url: str
    signals: list[tuple[str, str, str]] = field(default_factory=list)  # (field, value, note)
    inventory_url: str | None = None
    public_emails: list[str] = field(default_factory=list)
    pages_fetched: list[str] = field(default_factory=list)
    blocked_by_robots: bool = False


def _allowed(url: str, ua: str, client: httpx.Client) -> bool:
    parts = urlparse(url)
    robots = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        r = client.get(robots, headers={"User-Agent": ua}, timeout=10)
    except httpx.HTTPError:
        return False  # unreachable: RFC 9309 §2.3.1.4 says assume complete disallow
    if r.status_code >= 500 or r.status_code in (401, 403):
        return False  # 5xx = unreachable (disallow); 401/403 treated conservatively as a refusal
    if r.status_code >= 400:
        return True  # 4xx "unavailable": RFC 9309 §2.3.1.3 allows crawling
    rp.parse(r.text.splitlines())
    return rp.can_fetch(ua, url)


def research_site(url: str, *, client: httpx.Client | None = None, max_pages: int = 2) -> SiteObservation:
    s = get_settings()
    if not s.website_research_enabled:
        raise NotConfiguredError("Website research is disabled (DF_WEBSITE_RESEARCH_ENABLED=false)")
    ua = s.http_user_agent
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=20)
    obs = SiteObservation(url=url, final_url=url)
    try:
        if not _allowed(url, ua, client):
            obs.blocked_by_robots = True
            return obs
        try:
            resp = request("GET", url, provider="website", client=client, retries=1, rate=(1, 2.0))
        except PermanentError as exc:
            raise IntegrationError(f"website returned an error: {exc}") from exc
        final = str(resp.url)
        obs.final_url = final
        obs.pages_fetched.append(final)
        html = resp.text[:2_000_000]
        p = _Parser()
        p.feed(html)
        text = re.sub(r"<[^>]+>", " ", html)
        obs.signals.append(("signal:https", str(final.startswith("https://")).lower(), "Final URL scheme"))
        obs.signals.append(("signal:mobile_viewport", str(p.viewport).lower(), "meta viewport tag present"))
        obs.signals.append(("signal:contact_form", str(p.forms > 0).lower(), f"{p.forms} form(s) on homepage"))
        chat = any(re.search(r"(livechat|tawk|intercom|drift|podium|carchat|gubagoo|activengage)", sc, re.I) for sc in p.scripts)
        obs.signals.append(("signal:chat_widget", str(chat).lower(), "Known chat widget script on homepage"))
        credit = bool(re.search(r"(credit application|apply for financing|get pre-?approved|aplicar)", text, re.I))
        obs.signals.append(("signal:online_credit_app", str(credit).lower(), "Financing/credit application wording on homepage"))
        spanish = p.lang.startswith("es") or bool(SPANISH_HINTS.search(text))
        obs.signals.append(("signal:spanish_content", str(spanish).lower(),
                            "Spanish wording observed on homepage" if spanish else "No Spanish wording observed on homepage"))
        host = urlparse(final).netloc
        obs.public_emails = sorted({e for e in p.mailtos if "@" in e})
        inv = next((urljoin(final, h) for h, t in p.links
                    if INVENTORY_HINTS.search(h) or INVENTORY_HINTS.search(t or "")), None)
        if inv and urlparse(inv).netloc.endswith(host.removeprefix("www.")):
            obs.inventory_url = inv
            if max_pages > 1 and _allowed(inv, ua, client):
                try:
                    ir = request("GET", inv, provider="website", client=client, retries=1, rate=(1, 2.0))
                    obs.pages_fetched.append(str(ir.url))
                    count = len(set(m.group(0).lower() for m in VEHICLE_CARD.finditer(re.sub(r"<[^>]+>", " ", ir.text))))
                    if count:
                        obs.signals.append(("signal:inventory_count", str(count),
                                            "Distinct year+make mentions on the first inventory page (rough lower bound)"))
                except IntegrationError:
                    pass
        return obs
    finally:
        if own:
            client.close()
