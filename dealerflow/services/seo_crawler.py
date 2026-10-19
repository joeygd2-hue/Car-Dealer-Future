"""Polite, authorized website crawler.

* Honours robots.txt for our user agent (urllib.robotparser) and any Crawl-delay.
* Same-host only, HTML only, bounded page count, fixed delay between requests.
* Never submits forms, never authenticates, never bypasses access controls:
  401/403/429 responses are recorded and the crawl backs off (429 stops it).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from ..config import get_settings

HTML_TYPES = ("text/html", "application/xhtml+xml")
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4", ".css", ".js", ".ico", ".xml", ".json")


@dataclass
class FetchedPage:
    url: str
    status_code: int | None
    content_type: str = ""
    html: str = ""
    headers: dict = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int | None = None


@dataclass
class CrawlResult:
    pages: list[FetchedPage]
    robots_txt_found: bool
    robots_blocked_urls: list[str]
    sitemap_urls: list[str]
    sitemap_found: bool
    link_status: dict[str, int | None]
    stopped_reason: str | None = None


def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    path = p.path or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}" + (f"?{p.query}" if p.query else "")


def same_site(url: str, base: str) -> bool:
    a, b = urlparse(url).netloc.lower(), urlparse(base).netloc.lower()
    return a == b or a.removeprefix("www.") == b.removeprefix("www.")


class Crawler:
    def __init__(self, base_url: str, *, max_pages: int, delay_seconds: float, client: httpx.Client | None = None, sleep=time.sleep):
        s = get_settings()
        self.base = normalize(base_url if "://" in base_url else f"https://{base_url}")
        self.max_pages = max_pages
        self.delay = max(0.5, delay_seconds)
        self.ua = s.http_user_agent
        self.client = client or httpx.Client(
            timeout=s.crawler_timeout_seconds, follow_redirects=True, headers={"User-Agent": self.ua}
        )
        self.sleep = sleep
        self.robots = robotparser.RobotFileParser()
        self._last = 0.0

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if self._last and gap < self.delay:
            self.sleep(self.delay - gap)
        self._last = time.monotonic()

    def _get(self, url: str) -> httpx.Response | None:
        self._wait()
        try:
            return self.client.get(url, headers={"User-Agent": self.ua})
        except httpx.HTTPError:
            return None

    def load_robots(self) -> tuple[bool, list[str]]:
        robots_url = urljoin(self.base, "/robots.txt")
        resp = self._get(robots_url)
        if resp is not None and resp.status_code == 200:
            self.robots.parse(resp.text.splitlines())
            delay = self.robots.crawl_delay(self.ua)
            if delay:
                self.delay = max(self.delay, float(delay))
            return True, list(self.robots.site_maps() or [])
        if resp is not None and resp.status_code in (401, 403):
            # Per RFC 9309 an access-restricted robots.txt means: assume complete disallow.
            self.robots.disallow_all = True
        else:
            self.robots.allow_all = True
        return False, []

    def allowed(self, url: str) -> bool:
        return self.robots.can_fetch(self.ua, url)

    def load_sitemap(self, declared: list[str]) -> tuple[bool, list[str]]:
        from bs4 import BeautifulSoup

        candidates = declared or [urljoin(self.base, "/sitemap.xml")]
        urls: list[str] = []
        found = False
        for sm in candidates[:3]:
            if not self.allowed(sm):
                continue
            resp = self._get(sm)
            if resp is None or resp.status_code != 200:
                continue
            found = True
            soup = BeautifulSoup(resp.text, "html.parser")
            for loc in soup.find_all("loc")[:500]:
                urls.append(loc.get_text(strip=True))
        return found, urls

    def crawl(self, seed_urls: list[str] | None = None, check_links_limit: int = 100) -> CrawlResult:
        from bs4 import BeautifulSoup

        robots_found, declared_sitemaps = self.load_robots()
        sitemap_found, sitemap_urls = self.load_sitemap(declared_sitemaps)
        queue: deque[str] = deque([self.base])
        for u in (seed_urls or []) + sitemap_urls[:20]:
            if same_site(u, self.base):
                queue.append(normalize(u))
        seen: set[str] = set()
        pages: list[FetchedPage] = []
        blocked: list[str] = []
        discovered_links: set[str] = set()
        stopped = None
        while queue and len(pages) < self.max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if urlparse(url).path.lower().endswith(SKIP_EXT):
                continue
            if not self.allowed(url):
                blocked.append(url)
                continue
            t0 = time.monotonic()
            resp = self._get(url)
            if resp is None:
                pages.append(FetchedPage(url=url, status_code=None, error="request failed"))
                continue
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            page = FetchedPage(
                url=normalize(str(resp.url)), status_code=resp.status_code, content_type=ctype,
                headers={k.lower(): v for k, v in resp.headers.items() if k.lower() in ("x-robots-tag", "content-type", "content-language")},
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
            if resp.status_code == 429:
                stopped = "Site returned 429 Too Many Requests; crawl stopped to avoid overloading it"
                pages.append(page)
                break
            if resp.status_code == 200 and ctype in HTML_TYPES:
                page.html = resp.text[:2_000_000]
                soup = BeautifulSoup(page.html, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                        continue
                    absolute = normalize(urljoin(page.url, href))
                    if not absolute.startswith(("http://", "https://")):
                        continue
                    if same_site(absolute, self.base):
                        discovered_links.add(absolute)
                        if absolute not in seen:
                            queue.append(absolute)
            pages.append(page)

        # Check internal links that were discovered but not crawled (bounded, robots-respecting).
        link_status: dict[str, int | None] = {p.url: p.status_code for p in pages}
        unchecked = [u for u in sorted(discovered_links) if u not in link_status and self.allowed(u)]
        if stopped is None:
            for u in unchecked[:check_links_limit]:
                self._wait()
                try:
                    r = self.client.head(u, headers={"User-Agent": self.ua})
                    if r.status_code in (405, 501):
                        r = self.client.get(u, headers={"User-Agent": self.ua})
                    link_status[u] = r.status_code
                    if r.status_code == 429:
                        break
                except httpx.HTTPError:
                    link_status[u] = None
        return CrawlResult(
            pages=pages, robots_txt_found=robots_found, robots_blocked_urls=blocked, sitemap_urls=sitemap_urls,
            sitemap_found=sitemap_found, link_status=link_status, stopped_reason=stopped,
        )
