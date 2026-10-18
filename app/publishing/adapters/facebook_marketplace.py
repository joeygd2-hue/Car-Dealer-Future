"""Facebook Marketplace - MANUAL publishing workflow.

Research (docs/research/meta-and-inventory-integrations-2026-10.md): Meta ended
partner-catalog vehicle listings on Marketplace on 2021-09-13 and business-Page
vehicle listings on 2023-01-30. There is no API or partner program through
which we may create organic Marketplace vehicle listings, read Marketplace
buyer messages, or edit/delete those listings. Automating the personal-profile
UI would conflict with Meta's Terms, so we do not do it.

This adapter therefore never contacts Facebook. It prepares a complete listing
package (copy, fields, photo references, checklist) for a human to post, and
publication is recorded only when that person submits the live listing URL and
attests they posted it.
"""
from __future__ import annotations

import re
from typing import Any

from app.publishing.adapters.base import AdapterResult, ChannelAdapter

MARKETPLACE_URL_RE = re.compile(r"^https://(www\.|m\.|web\.)?facebook\.com/marketplace/item/\d{5,}/?(\?.*)?$")

CHECKLIST = [
    "Sign in to Facebook as the dealership staff member who will own and manage this listing (no shared or purchased accounts).",
    "Open Marketplace > Create new listing > Vehicle for sale.",
    "Upload the photos below in the order shown (download them from the dealership's photo links).",
    "Copy each field from this package exactly. Do not add claims (warranty, accident history, financing) that are not in the package.",
    "Paste the description in the language you are posting (English or Spanish). Post a second listing only if the dealership wants both.",
    "Review the preview against the vehicle data, then publish.",
    "Copy the live listing URL (facebook.com/marketplace/item/...) and record it here to confirm publication.",
    "When the vehicle sells or the price changes, update or delete the Facebook listing first, then record that here.",
]


class FacebookMarketplaceManualAdapter(ChannelAdapter):
    key = "facebook_marketplace"
    label = "Facebook Marketplace (manual publishing)"
    mode = "manual"
    requires_connection = False
    DOCS = {
        "integration": "None - Meta offers no supported API for organic Marketplace vehicle listings (ended 2021/2023).",
        "supported_operations": ["prepare listing package", "export/copy", "record human publication", "record edit", "record removal"],
        "credentials": "None stored. A dealership staff member posts from their own Facebook account.",
        "fields": ["year", "make", "model", "mileage", "price", "body style", "exterior/interior color", "fuel type",
                   "transmission", "condition", "title", "description", "location", "photos"],
        "media": "Photo URLs from the dealer's inventory feed; the human uploads the images.",
        "rate_limits": "Not applicable (human posting).",
        "confirmation": "Manual attestation plus the live listing URL; status 'published_confirmed' with confirmation_type "
                        "'manual_attested'. Never counted as an API-confirmed publication.",
        "update_delete": "Human edits/deletes on Facebook, then records it here. Sold vehicles are flagged 'withdrawal_required'.",
        "failure_recovery": "Not applicable; the package can be regenerated at any time.",
        "messages": "Marketplace buyer messages cannot be read via API (REPORTED). Phase 3 must treat them as manual.",
        "status": "CONFIRMED unsupported for automation; manual workflow implemented.",
    }

    def build_payload(self, *, content: dict, vehicle, facts: dict, connection_config: dict) -> dict[str, Any]:
        v = facts["vehicle"]
        loc = facts["location"]
        package = {
            "channel": self.key,
            "automation": "none - manual publication required",
            "fields": {
                "vehicle_type": "Car/Truck",
                "year": v.get("year"), "make": v.get("make"), "model": v.get("model"), "trim": v.get("trim"),
                "mileage": v.get("mileage"), "price": facts.get("price_usd"), "body_style": v.get("body_style"),
                "exterior_color": v.get("exterior_color"), "interior_color": v.get("interior_color"),
                "fuel_type": v.get("fuel_type"), "transmission": v.get("transmission"), "condition": v.get("condition"),
                "vin": v.get("vin"),
                "location": ", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("postal")) if x) or None,
                # Only state title status if the dealer supplied it.
                "title_status_reported_by_dealer": v.get("title_status"),
            },
            "copy": {lang: {"title": content[lang]["title"], "description": content[lang]["description"]}
                     for lang in ("en", "es") if lang in content},
            "photos": facts["photos"],
            "checklist": CHECKLIST,
            "notes": [
                "Field names on Facebook's form may change; match by meaning.",
                "Leave optional fields blank when the package has no value; do not guess.",
            ],
        }
        return package

    def validate_payload(self, payload: dict[str, Any]) -> list[str]:
        problems = []
        f = payload["fields"]
        for k in ("year", "make", "model", "price"):
            if not f.get(k):
                problems.append(f"{k} is required for a Marketplace vehicle listing")
        if not payload["photos"]:
            problems.append("At least one photo is required")
        return problems

    def publish(self, payload: dict, *, connection, idempotency_key: str) -> AdapterResult:
        return AdapterResult(outcome="manual_required", response={"package_ready": True})

    def update(self, payload: dict, *, connection, idempotency_key: str, external_id: str | None) -> AdapterResult:
        return AdapterResult(outcome="manual_required", response={"package_ready": True, "action": "edit_existing_listing"})

    def delete(self, *, connection, external_id: str | None, idempotency_key: str, payload: dict) -> AdapterResult:
        return AdapterResult(outcome="manual_required", response={"action": "delete_listing_on_facebook"})
