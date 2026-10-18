"""SEO checks. Each finding records whether it was directly measured or is a heuristic opportunity.

No check claims an effect on rankings, traffic or sales; impacts are phrased as
likely consequences per search-engine documentation.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.seo.crawler import CrawlResult, FetchedPage, normalize, same_site

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
PRICE_RE = re.compile(r"\$\s?\d{1,3}(,\d{3})+|\$\s?\d{4,6}")
INVENTORY_PATH_RE = re.compile(r"/(inventory|vehicle|vehicles|used|used-cars|cars|vdp|detail|details)(/|-|$)", re.I)
SPANISH_HINTS = re.compile(r"\b(financiamiento|vehículos|autos usados|se habla español|hablamos español|carros usados|crédito)\b", re.I)


@dataclass
class Finding:
    code: str
    finding: str
    evidence: str
    url: str | None
    severity: str
    impact: str
    recommended_fix: str
    effort: str
    responsible_party: str
    detection: str = "measured"
    current_value: str | None = None


def parse_page(page: FetchedPage) -> dict:
    """Extract the facts we check from one HTML page."""
    soup = BeautifulSoup(page.html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    robots = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    canonical = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
    html_tag = soup.find("html")
    headings = [(int(h.name[1]), h.get_text(" ", strip=True)[:120]) for h in soup.find_all(re.compile("^h[1-6]$"))]
    imgs = soup.find_all("img")
    missing_alt = [i.get("src", "")[:200] for i in imgs if not (i.get("alt") or "").strip()]
    jsonld_types: list[str] = []
    jsonld_objects: list[dict] = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            jsonld_types.append("INVALID_JSON")
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict):
                t = it.get("@type")
                for tt in t if isinstance(t, list) else [t]:
                    if tt:
                        jsonld_types.append(str(tt))
                jsonld_objects.append(it)
    hreflangs = [link.get("hreflang") for link in soup.find_all("link", hreflang=True)]
    text = soup.get_text(" ", strip=True)
    links = [a["href"] for a in soup.find_all("a", href=True)]
    return {
        "title": title,
        "meta_description": md.get("content", "").strip() if md else None,
        "meta_robots": robots.get("content", "").lower() if robots else "",
        "x_robots_tag": page.headers.get("x-robots-tag", "").lower(),
        "canonical": canonical.get("href") if canonical else None,
        "lang": html_tag.get("lang") if html_tag else None,
        "hreflang": hreflangs,
        "headings": headings,
        "images": len(imgs),
        "images_missing_alt": missing_alt,
        "jsonld_types": jsonld_types,
        "jsonld": jsonld_objects[:10],
        "word_count": len(text.split()),
        "text_sample": text[:20000],
        "links": links[:1000],
        "has_vin": bool(VIN_RE.search(text)),
        "has_price": bool(PRICE_RE.search(text)),
        "spanish_hints": bool(SPANISH_HINTS.search(text)) or (html_tag is not None and (html_tag.get("lang") or "").lower().startswith("es")),
    }


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def run_checks(crawl: CrawlResult, base_url: str, business: dict) -> tuple[list[Finding], dict[str, dict]]:
    findings: list[Finding] = []
    parsed: dict[str, dict] = {}
    html_pages = [p for p in crawl.pages if p.status_code == 200 and p.html]
    for p in html_pages:
        parsed[p.url] = parse_page(p)

    # --- site-level -----------------------------------------------------
    if not crawl.robots_txt_found:
        findings.append(Finding("robots_missing", "No robots.txt file found", f"GET {base_url.rstrip('/')}/robots.txt did not return 200", f"{base_url.rstrip('/')}/robots.txt", "low",
                                "Crawlers will assume everything may be crawled; no sitemap is advertised to them.", "Publish a robots.txt that references the XML sitemap.", "small", "web_provider"))
    if not crawl.sitemap_found:
        findings.append(Finding("sitemap_missing", "No XML sitemap found", "No sitemap declared in robots.txt and /sitemap.xml was not available", f"{base_url.rstrip('/')}/sitemap.xml", "medium",
                                "Search engines may discover new inventory pages more slowly.", "Generate an XML sitemap that includes inventory detail pages and reference it in robots.txt.", "small", "web_provider"))
    if crawl.robots_blocked_urls and base_url.rstrip("/") + "/" in crawl.robots_blocked_urls:
        findings.append(Finding("robots_blocks_home", "robots.txt blocks the homepage for crawlers", "robots.txt disallows the site root for our user agent", base_url, "critical",
                                "If this also applies to search engines, the site cannot be crawled.", "Review robots.txt Disallow rules with the website provider.", "small", "web_provider"))

    # --- page-level -----------------------------------------------------
    for url, d in parsed.items():
        title, desc = d["title"], d["meta_description"]
        if not title:
            findings.append(Finding("title_missing", "Page has no <title>", "No <title> element in the HTML", url, "high",
                                    "Search results may show an auto-generated title.", "Add a unique, descriptive title (dealership name, city, page topic).", "small", "web_provider"))
        elif len(title) > 65:
            findings.append(Finding("title_long", "Title is longer than ~65 characters", f"Title ({len(title)} chars): {title[:120]}", url, "low",
                                    "Search engines may truncate the title in results.", "Shorten the title to roughly 50–60 characters.", "small", "web_provider", current_value=title))
        elif len(title) < 15:
            findings.append(Finding("title_short", "Title is very short", f"Title: {title}", url, "low",
                                    "Short titles give searchers little context.", "Describe the page, e.g. 'Used Trucks for Sale in Sugar Land, TX | Dealer'.", "small", "web_provider", current_value=title))
        if not desc:
            findings.append(Finding("meta_description_missing", "Page has no meta description", "No <meta name=\"description\">", url, "medium",
                                    "Search engines generate a snippet from page text, which may be less relevant.", "Add a 120–160 character description of the page.", "small", "web_provider"))
        if "noindex" in d["meta_robots"] or "noindex" in d["x_robots_tag"]:
            findings.append(Finding("noindex", "Page is marked noindex", f"meta robots='{d['meta_robots']}' x-robots-tag='{d['x_robots_tag']}'", url, "high" if urlparse(url).path in ("", "/") else "medium",
                                    "The page is excluded from search indexes.", "Confirm whether exclusion is intended; remove noindex if not.", "small", "web_provider", current_value=d["meta_robots"] or d["x_robots_tag"]))
        h1s = [h for lvl, h in d["headings"] if lvl == 1]
        if len(h1s) == 0:
            findings.append(Finding("h1_missing", "Page has no H1 heading", "No <h1> element found", url, "medium",
                                    "The main topic of the page is less clear to readers and crawlers.", "Add one H1 describing the page.", "small", "web_provider"))
        elif len(h1s) > 1:
            findings.append(Finding("h1_multiple", "Page has multiple H1 headings", f"{len(h1s)} H1s: " + " | ".join(h1s[:4]), url, "low",
                                    "Heading structure is ambiguous.", "Use a single H1 and H2–H6 for sub-sections.", "small", "web_provider"))
        levels = [lvl for lvl, _ in d["headings"]]
        skips = [f"H{a}→H{b}" for a, b in zip(levels, levels[1:]) if b > a + 1]
        if skips:
            findings.append(Finding("heading_skip", "Heading levels skip", "Skipped levels: " + ", ".join(sorted(set(skips))[:5]), url, "info",
                                    "Skipped levels make the outline harder to follow for assistive technology.", "Use sequential heading levels.", "small", "web_provider"))
        if d["images_missing_alt"]:
            n = len(d["images_missing_alt"])
            findings.append(Finding("img_alt_missing", f"{n} image(s) missing alt text", "Examples: " + ", ".join(d["images_missing_alt"][:3]), url, "medium" if n > 5 else "low",
                                    "Images are not described to screen readers or image search.", "Add alt text describing each vehicle photo (year, make, model, view).", "medium" if n > 20 else "small", "web_provider", current_value=str(n)))
        if "INVALID_JSON" in d["jsonld_types"]:
            findings.append(Finding("jsonld_invalid", "Structured data block is not valid JSON", "A <script type=application/ld+json> failed to parse", url, "medium",
                                    "Search engines ignore invalid structured data.", "Fix the JSON-LD syntax and validate it with the Rich Results Test.", "small", "web_provider"))
        is_inventory = bool(INVENTORY_PATH_RE.search(urlparse(url).path)) or any(t in ("Car", "Vehicle", "Product") for t in d["jsonld_types"])
        if is_inventory and (d["has_vin"] or d["has_price"]):
            if not any(t in ("Car", "Vehicle", "Product", "Offer") for t in d["jsonld_types"]):
                findings.append(Finding("inventory_schema_missing", "Vehicle page has no Vehicle/Car structured data", "Page looks like a vehicle detail page (VIN or price present) but has no Car/Vehicle JSON-LD", url, "medium",
                                        "Vehicle details are less machine-readable for search features.", "Add schema.org Car (with vehicleIdentificationNumber, offers.price) to vehicle pages.", "medium", "web_provider"))
            if not d["has_price"]:
                findings.append(Finding("inventory_price_missing", "Vehicle page shows no price", "No dollar amount detected on the page", url, "low",
                                        "Shoppers comparing listings may skip vehicles without a visible price.", "Display the price or a clear call to action when price is withheld.", "small", "dealership"))

    # Duplicates
    for field, code, label in (("title", "title_duplicate", "title"), ("meta_description", "meta_description_duplicate", "meta description")):
        counts = Counter(d[field] for d in parsed.values() if d[field])
        for value, n in counts.items():
            if n > 1:
                urls = [u for u, d in parsed.items() if d[field] == value]
                findings.append(Finding(code, f"Duplicate {label} on {n} pages", f"'{value[:100]}' used on: " + ", ".join(urls[:5]), urls[0], "medium",
                                        f"Pages with identical {label}s are hard to tell apart in results.", f"Write a unique {label} for each page.", "medium", "web_provider", current_value=value[:200]))

    # Broken internal links
    inbound: dict[str, int] = defaultdict(int)
    referrers_of: dict[str, list[str]] = defaultdict(list)
    for url, d in parsed.items():
        for href in d["links"]:
            target = normalize(urljoin(url, href))
            if same_site(target, base_url) and target != url:
                inbound[target] += 1
                if url not in referrers_of[target]:
                    referrers_of[target].append(url)
    for target, status in crawl.link_status.items():
        if status is not None and status >= 400 and status not in (401, 403, 429):
            referrers = referrers_of.get(target, [])
            findings.append(Finding("broken_link", f"Internal link returns HTTP {status}", f"{target} returned {status}" + (f"; linked from {referrers[0]}" if referrers else ""), target, "high" if status == 404 else "medium",
                                    "Visitors and crawlers reach an error page.", "Fix or remove the link, or redirect the URL to the right page.", "small", "web_provider", current_value=str(status)))
    # Weak internal linking (pages found via sitemap but never linked)
    home = base_url.rstrip("/") + "/"
    for url in parsed:
        if url != home and inbound.get(url, 0) == 0 and url in set(crawl.sitemap_urls):
            findings.append(Finding("orphan_page", "Page has no internal links pointing to it", "Found in the sitemap but not linked from any crawled page", url, "low",
                                    "Pages without internal links are harder for visitors and crawlers to find.", "Link to the page from relevant category or navigation pages.", "small", "web_provider", detection="heuristic"))

    # Local business consistency (NAP) — compared with the business details on file.
    home_data = parsed.get(home) or (next(iter(parsed.values())) if parsed else None)
    if home_data is not None:
        types = set(home_data["jsonld_types"])
        if not types & {"AutoDealer", "LocalBusiness", "AutomotiveBusiness", "Organization"}:
            findings.append(Finding("local_schema_missing", "No AutoDealer/LocalBusiness structured data on the homepage", "Homepage JSON-LD types: " + (", ".join(sorted(types)) or "none"), home, "medium",
                                    "Name, address, phone and hours are less machine-readable for local search.", "Add schema.org AutoDealer markup with name, address, telephone and openingHours.", "small", "web_provider"))
        phone = _digits(business.get("phone"))
        if phone and phone not in _digits_all(home_data["text_sample"]):
            findings.append(Finding("nap_phone_mismatch", "Phone number on file not found on the homepage", f"Expected phone ending {phone[-4:]} not found in homepage text", home, "medium",
                                    "Inconsistent contact details can confuse customers and local listings.", "Confirm the correct number and show it consistently on the site and listings.", "small", "dealership", current_value=None))
        for obj in home_data["jsonld"]:
            tel = _digits(obj.get("telephone")) if isinstance(obj, dict) else ""
            if phone and tel and tel != phone:
                findings.append(Finding("nap_schema_phone_mismatch", "Structured-data phone differs from the phone on file", f"JSON-LD telephone ends {tel[-4:]}, business record ends {phone[-4:]}", home, "medium",
                                        "Conflicting phone numbers across sources.", "Update the JSON-LD telephone to the correct number.", "small", "web_provider", current_value=tel[-4:]))
        name = (business.get("name") or "").lower()
        if name and name.split()[0] not in home_data["text_sample"].lower():
            findings.append(Finding("nap_name_missing", "Business name on file not found on the homepage", f"'{business.get('name')}' not found in homepage text", home, "low",
                                    "Customers may not recognise the business from the page.", "Show the legal/trading name consistently.", "small", "dealership"))

    # Bilingual opportunity — a heuristic, not a measured defect.
    if parsed and not any(d["spanish_hints"] for d in parsed.values()) and not any(h and h.lower().startswith("es") for d in parsed.values() for h in d["hreflang"]):
        findings.append(Finding("spanish_content_opportunity", "No Spanish-language content detected", f"None of {len(parsed)} crawled pages declared lang=es, hreflang=es or contained common Spanish automotive terms", home, "info",
                                "Spanish-speaking shoppers may not find content in their language. (Opportunity, not an error.)", "Consider Spanish versions of key pages (inventory, financing, contact) with hreflang annotations.", "large", "our_team", detection="heuristic"))
    return findings, parsed


def _digits_all(text: str) -> str:
    return re.sub(r"\D", "", text or "")
