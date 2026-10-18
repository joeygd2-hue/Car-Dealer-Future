"""Vehicle validation: missing, contradictory and implausible data."""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse

from app.inventory.vin import model_years_for, vin_problems

REQUIRED_FOR_LISTING = ("year", "make", "model")


def _issue(code, field, severity, message):
    return {"code": code, "field": field, "severity": severity, "message": message}


def validate_vehicle(rec: dict[str, Any]) -> list[dict]:
    issues: list[dict] = []
    for f in REQUIRED_FOR_LISTING:
        if not rec.get(f):
            issues.append(_issue("missing_required", f, "error", f"{f.title()} is missing; a listing cannot be generated"))
    if rec.get("price") is None:
        issues.append(_issue("missing_price", "price", "warning", "Price is missing; most channels require a price"))
    if rec.get("mileage") is None:
        issues.append(_issue("missing_mileage", "mileage", "warning", "Mileage is missing"))
    if not rec.get("photos"):
        issues.append(_issue("missing_photos", "photos", "warning", "No photos supplied"))
    if not rec.get("vin"):
        issues.append(_issue("missing_vin", "vin", "warning", "VIN not supplied"))
    if not rec.get("exterior_color"):
        issues.append(_issue("missing_color", "exterior_color", "warning", "Exterior color is missing"))

    vin = rec.get("vin")
    if vin:
        for p in vin_problems(vin):
            issues.append(_issue("vin_invalid", "vin", "warning", p))
        years = model_years_for(vin) if not vin_problems(vin) else []
        if years and rec.get("year") and rec["year"] not in years:
            issues.append(_issue("vin_year_mismatch", "year", "error",
                                 f"Year {rec['year']} contradicts the VIN's model-year code ({' or '.join(map(str, years))})"))

    m, p, y = rec.get("mileage"), rec.get("price"), rec.get("year")
    if m is not None and m > 500_000:
        issues.append(_issue("implausible_mileage", "mileage", "warning", f"Mileage {m:,} is unusually high; please confirm"))
    if p is not None and (p < 500_00 or p > 500_000_00):
        issues.append(_issue("implausible_price", "price", "warning", f"Price ${p / 100:,.0f} looks implausible; please confirm"))
    if rec.get("condition") == "new" and m is not None and m > 1000:
        issues.append(_issue("condition_mileage_conflict", "condition", "error",
                             f"Condition is 'new' but mileage is {m:,}"))
    if y and m is not None and y >= date.today().year - 1 and m > 150_000:
        issues.append(_issue("year_mileage_conflict", "mileage", "warning", f"{m:,} miles on a {y} vehicle; please confirm"))
    if rec.get("fuel_type") == "electric" and rec.get("transmission") == "manual":
        issues.append(_issue("fuel_transmission_conflict", "transmission", "warning", "Electric vehicle listed with a manual transmission"))
    for url in rec.get("photos") or []:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            issues.append(_issue("invalid_photo_url", "photos", "warning", f"Photo reference is not a web URL: {url[:80]}"))
        elif u.scheme == "http":
            issues.append(_issue("insecure_photo_url", "photos", "warning", "Photo URL uses http; most channels require https"))
    if rec.get("title_status") in {"salvage", "rebuilt", "flood", "lemon_buyback"}:
        issues.append(_issue("branded_title", "title_status", "warning",
                             f"Branded title ({rec['title_status']}); listings must disclose it"))
    return issues


def status_from_issues(issues: list[dict]) -> str:
    if any(i["severity"] == "error" for i in issues):
        return "invalid"
    return "warnings" if issues else "valid"
