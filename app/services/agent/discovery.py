"""Prospect discovery from permitted sources, with de-duplication.

Sources:
* csv            — a research list prepared by staff (manual, permitted).
* google_places  — Google Places API (New) Text Search, official API with key.
                   Places content is subject to Google Maps Platform terms
                   (attribution and caching limits). We store the place id as
                   `source_ref`; refresh details from the API rather than
                   treating stored copies as permanent.
No scraping of directories or sites that prohibit it; no CAPTCHA bypass.
"""
from __future__ import annotations

import csv
import io
import re
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.outreach import Prospect
from app.services import costs

PLACES_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.websiteUri,places.nationalPhoneNumber,places.types,places.businessStatus,nextPageToken"

# Brand words that suggest a franchise (new-car) dealership rather than an independent one.
FRANCHISE_HINTS = re.compile(
    r"\b(toyota|honda|ford|chevrolet|chevy|nissan|hyundai|kia|bmw|mercedes|audi|lexus|acura|infiniti|subaru|mazda|"
    r"volkswagen|vw|jeep|dodge|ram|chrysler|gmc|buick|cadillac|lincoln|volvo|porsche|tesla|carmax|carvana|autonation)\b",
    re.I,
)


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return host.removeprefix("www.") or None


def dedupe_key(name: str, website: str | None, phone: str | None, postal_code: str | None) -> str:
    d = domain_of(website)
    if d:
        return f"domain:{d}"
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) == 10:
        return f"phone:{digits}"
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    return f"name:{norm}:{(postal_code or '').strip()}"


def upsert_prospect(db: Session, *, source: str, business_name: str, **fields) -> tuple[Prospect, bool]:
    key = dedupe_key(business_name, fields.get("website"), fields.get("phone"), fields.get("postal_code"))
    existing = db.scalar(select(Prospect).where(Prospect.dedupe_key == key))
    if existing:
        # Fill gaps only; never overwrite verified data with new unverified data.
        for k, v in fields.items():
            if v and not getattr(existing, k, None):
                setattr(existing, k, v)
        return existing, False
    p = Prospect(business_name=business_name.strip()[:200], source=source, dedupe_key=key, stage="new", **fields)
    if FRANCHISE_HINTS.search(business_name):
        p.is_independent = False
    db.add(p)
    db.flush()
    return p, True


def import_csv(db: Session, content: str) -> dict:
    """Columns: business_name, website, phone, email, address, city, state, postal_code, contact_name, email_source_url."""
    created = dupes = 0
    errors: list[str] = []
    for i, row in enumerate(csv.DictReader(io.StringIO(content)), start=2):
        name = (row.get("business_name") or "").strip()
        if not name:
            errors.append(f"line {i}: business_name is required")
            continue
        email = (row.get("email") or "").strip().lower() or None
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
            errors.append(f"line {i}: invalid email")
            email = None
        if email and not (row.get("email_source_url") or "").strip():
            errors.append(f"line {i}: email has no source URL; stored without email (facts must be evidenced)")
            email = None
        _, new = upsert_prospect(
            db, source="csv", business_name=name, website=(row.get("website") or "").strip() or None,
            phone=(row.get("phone") or "").strip() or None, email=email,
            email_source_url=(row.get("email_source_url") or "").strip() or None,
            address_line1=(row.get("address") or "").strip() or None, city=(row.get("city") or "").strip() or None,
            state=(row.get("state") or "").strip() or None, postal_code=(row.get("postal_code") or "").strip() or None,
            contact_name=(row.get("contact_name") or "").strip() or None,
        )
        created += new
        dupes += not new
    return {"created": created, "duplicates": dupes, "errors": errors}


def places_search(db: Session, query: str, client: httpx.Client | None = None, page_token: str | None = None) -> dict:
    key = get_settings().google_places_api_key
    if not key:
        return {"skipped": "GOOGLE_PLACES_API_KEY not configured"}
    c = client or httpx.Client(timeout=30)
    body = {"textQuery": query, "pageSize": 20, "includedType": "car_dealer"}
    if page_token:
        body["pageToken"] = page_token
    r = c.post(PLACES_ENDPOINT, json=body, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELD_MASK})
    costs.record_usage(db, provider="google:places_text_search", service_category="data_api", metric="requests", quantity=1, feature="agent.discovery")
    if r.status_code != 200:
        raise httpx.HTTPStatusError(f"Places API returned {r.status_code}", request=r.request, response=r)
    data = r.json()
    created = dupes = 0
    for place in data.get("places", []):
        if place.get("businessStatus") not in (None, "OPERATIONAL"):
            continue
        name = (place.get("displayName") or {}).get("text")
        if not name:
            continue
        addr = place.get("formattedAddress") or ""
        m = re.search(r",\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})", addr)
        _, new = upsert_prospect(
            db, source="google_places", business_name=name, source_ref=place.get("id"), website=place.get("websiteUri"),
            phone=place.get("nationalPhoneNumber"), address_line1=addr.split(",")[0] if addr else None,
            city=m.group(1).strip() if m else None, state=m.group(2) if m else None, postal_code=m.group(3) if m else None,
        )
        created += new
        dupes += not new
    return {"created": created, "duplicates": dupes, "next_page_token": data.get("nextPageToken")}
