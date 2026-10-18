"""Normalisation and validation of dealership data."""

from __future__ import annotations

import re
from urllib.parse import urlparse

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR",
    "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")
_SUFFIXES = re.compile(r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|ltd|lp|dba)\b\.?", re.I)
_FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com", "msn.com",
              "att.net", "sbcglobal.net", "comcast.net", "protonmail.com", "ymail.com"}


def normalize_name(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = _SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_url(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return url


def domain_of(url_or_email: str | None) -> str | None:
    if not url_or_email:
        return None
    s = url_or_email.strip().lower()
    if "@" in s and "://" not in s:
        d = s.rsplit("@", 1)[1]
    else:
        u = normalize_url(s)
        if not u:
            return None
        d = urlparse(u).netloc.split(":")[0]
    d = d.removeprefix("www.")
    return d or None


def is_free_mail_domain(domain: str | None) -> bool:
    return bool(domain) and domain in _FREE_MAIL


def normalize_phone(phone: str | None) -> str | None:
    """Return E.164 for a US/Canada number, else None."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone.split(" ext")[0].split(" x")[0])
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return None
    return "+1" + digits


def format_phone(e164: str | None) -> str | None:
    if not e164 or len(e164) != 12:
        return e164
    d = e164[2:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}"


def valid_email(email: str | None) -> bool:
    return bool(email) and bool(_EMAIL_RE.match(email.strip()))


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    return e if valid_email(e) else None


def valid_zip(z: str | None) -> bool:
    return bool(z) and bool(_ZIP_RE.match(z.strip()))


def validate_prospect_fields(data: dict) -> tuple[dict, list[str]]:
    """Return (cleaned, errors). Invalid optional values are dropped and reported, never guessed."""
    errors: list[str] = []
    out = dict(data)
    name = (data.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    out["name"] = name[:255]
    if data.get("website"):
        w = normalize_url(data["website"])
        if not w:
            errors.append(f"website is not a valid URL: {data['website']!r}")
        out["website"] = w
    if data.get("inventory_url"):
        w = normalize_url(data["inventory_url"])
        if not w:
            errors.append(f"inventory_url is not a valid URL: {data['inventory_url']!r}")
        out["inventory_url"] = w
    if data.get("email"):
        e = normalize_email(data["email"])
        if not e:
            errors.append(f"email is not valid: {data['email']!r}")
        out["email"] = e
    if data.get("phone"):
        p = normalize_phone(data["phone"])
        if not p:
            errors.append(f"phone is not a valid US number: {data['phone']!r}")
        out["phone_e164"] = p
    if data.get("state"):
        st = data["state"].strip().upper()
        if st in ("TEXAS",):
            st = "TX"
        if st not in US_STATES:
            errors.append(f"state is not a US state code: {data['state']!r}")
            st = None
        out["state"] = st
    if data.get("postal_code"):
        z = str(data["postal_code"]).strip()
        if not valid_zip(z):
            errors.append(f"postal_code is not a valid ZIP: {z!r}")
            z = None
        out["postal_code"] = z[:5] if z else None
    for key in ("lat", "lng"):
        if data.get(key) not in (None, ""):
            try:
                out[key] = float(data[key])
            except (TypeError, ValueError):
                errors.append(f"{key} is not a number")
                out[key] = None
    return out, errors


KEY_FIELDS = ("website", "address_line", "city", "postal_code", "phone_e164", "email", "inventory_url")


def missing_fields(prospect) -> list[str]:
    missing = [f for f in KEY_FIELDS if not getattr(prospect, f, None)]
    if not any(c.verified and (c.email or c.phone) for c in getattr(prospect, "contacts", [])):
        missing.append("verified_contact")
    if getattr(prospect, "dealership_type", "unknown") == "unknown":
        missing.append("dealership_type")
    return missing
