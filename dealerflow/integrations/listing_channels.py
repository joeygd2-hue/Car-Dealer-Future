"""Listing publishing channels (Phase 2).

Each channel documents its real capabilities in ``DOCS`` (shown in the UI and in
docs/architecture/integration-matrix.md).

mode:
  api     - a real, authorised API (may still need credentials / app review before it works)
  manual  - we prepare everything; a person publishes and records the result
  mock    - SIMULATED API for tests and demos; never reaches a platform

Research: docs/research/meta-and-inventory-integrations-2026-10.md
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..audit import track_integration
from ..config import get_settings
from .errors import CredentialError, IntegrationError, NotConfiguredError, PermanentError
from .http import request


@dataclass
class ChannelResult:
    outcome: str  # confirmed | submitted | manual_required | failed
    external_id: str | None = None
    external_url: str | None = None
    provider_ref: str | None = None
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ListingChannel:
    key = "base"
    label = "Base"
    mode = "manual"
    simulated = False
    DOCS: dict[str, Any] = {}

    def build_payload(self, *, content: dict, vehicle, facts: dict, connection_config: dict) -> dict[str, Any]:
        raise NotImplementedError

    def validate_payload(self, payload: dict[str, Any]) -> list[str]:
        return []

    def publish(self, db, payload: dict, *, connection, idempotency_key: str) -> ChannelResult:
        raise NotImplementedError

    def update(self, db, payload: dict, *, connection, idempotency_key: str, external_id: str | None) -> ChannelResult:
        return self.publish(db, payload, connection=connection, idempotency_key=idempotency_key)

    def delete(self, db, *, connection, external_id: str | None, idempotency_key: str, payload: dict) -> ChannelResult:
        raise NotImplementedError

    def check_status(self, db, *, connection, provider_ref: str, payload: dict) -> ChannelResult:
        return ChannelResult(outcome="confirmed")


# --- Facebook Marketplace: manual ----------------------------------------------------------------

MARKETPLACE_URL_RE = re.compile(r"^https://(www\.|m\.|web\.)?facebook\.com/marketplace/item/\d{5,}/?(\?.*)?$")

MARKETPLACE_CHECKLIST = [
    "Sign in to Facebook as the dealership staff member who will own and manage this listing (no shared or purchased accounts).",
    "Open Marketplace > Create new listing > Vehicle for sale.",
    "Upload the photos below in the order shown (download them from the dealership's photo links).",
    "Copy each field from this package exactly. Do not add claims (warranty, accident history, financing) that are not in the package.",
    "Paste the description in the language you are posting (English or Spanish). Post a second listing only if the dealership wants both.",
    "Review the preview against the vehicle data, then publish.",
    "Copy the live listing URL (facebook.com/marketplace/item/...) and record it here to confirm publication.",
    "When the vehicle sells or the price changes, update or delete the Facebook listing first, then record that here.",
]


class FacebookMarketplaceManual(ListingChannel):
    """Meta ended partner-catalog vehicle listings on Marketplace (2021-09-13) and business-Page vehicle listings
    (2023-01-30). There is no API through which we may create, edit or delete organic Marketplace vehicle listings
    or read buyer messages, and automating the personal-profile UI would conflict with Meta's Terms. This channel
    never contacts Facebook: it prepares a package and records what a person did."""

    key = "facebook_marketplace"
    label = "Facebook Marketplace (manual publishing)"
    mode = "manual"
    DOCS = {
        "integration": "None. Meta offers no supported API for organic Marketplace vehicle listings (ended 2021/2023).",
        "supported_operations": ["prepare listing package", "copy/export", "record human publication", "record edit", "record removal"],
        "credentials": "None stored. A dealership staff member posts from their own Facebook account.",
        "fields": ["year", "make", "model", "mileage", "price", "body style", "colors", "fuel type", "transmission",
                   "condition", "title", "description", "location", "photos"],
        "media": "Photo URLs from the dealer's inventory feed; the person uploads the images.",
        "rate_limits": "Not applicable (human posting).",
        "confirmation": "Staff attestation plus the live listing URL (confirmation_type manual_attested). Never counted as an API publication.",
        "update_delete": "A person edits/deletes on Facebook, then records it here. Sold vehicles are flagged withdrawal_required.",
        "failure_recovery": "Not applicable; the package can be regenerated at any time.",
        "messages": "Marketplace buyer messages cannot be read via API (REPORTED). Phase 3 must treat them as manual.",
        "status": "CONFIRMED unsupported for automation; manual workflow implemented.",
    }

    def build_payload(self, *, content, vehicle, facts, connection_config):
        v, loc = facts["vehicle"], facts["location"]
        return {
            "channel": self.key,
            "automation": "none - manual publication required",
            "fields": {
                "vehicle_type": "Car/Truck", "year": v.get("year"), "make": v.get("make"), "model": v.get("model"),
                "trim": v.get("trim"), "mileage": v.get("mileage"), "price": facts.get("price_usd"),
                "body_style": v.get("body_style"), "exterior_color": v.get("exterior_color"),
                "interior_color": v.get("interior_color"), "fuel_type": v.get("fuel_type"), "transmission": v.get("transmission"),
                "condition": v.get("condition"), "vin": v.get("vin"),
                "location": ", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("postal")) if x) or None,
                "title_status_reported_by_dealer": v.get("title_status"),
            },
            "copy": {lang: {"title": content[lang]["title"], "description": content[lang]["description"]}
                     for lang in ("en", "es") if lang in content},
            "photos": facts["photos"],
            "checklist": MARKETPLACE_CHECKLIST,
            "notes": ["Field names on Facebook's form may change; match by meaning.",
                      "Leave optional fields blank when the package has no value; do not guess."],
        }

    def validate_payload(self, payload):
        problems = [f"{k} is required for a Marketplace vehicle listing" for k in ("year", "make", "model", "price")
                    if not payload["fields"].get(k)]
        if not payload["photos"]:
            problems.append("At least one photo is required")
        return problems

    def publish(self, db, payload, *, connection, idempotency_key):
        return ChannelResult(outcome="manual_required", response={"package_ready": True})

    def update(self, db, payload, *, connection, idempotency_key, external_id):
        return ChannelResult(outcome="manual_required", response={"package_ready": True, "action": "edit_existing_listing"})

    def delete(self, db, *, connection, external_id, idempotency_key, payload):
        return ChannelResult(outcome="manual_required", response={"action": "delete_listing_on_facebook"})


# --- Meta vehicle catalog (Automotive Inventory Ads): API ----------------------------------------

STATE_OF_VEHICLE = {"used": "used", "new": "new", "certified_pre_owned": "cpo"}
META_FUEL = {"gasoline": "gasoline", "diesel": "diesel", "hybrid": "hybrid", "plug_in_hybrid": "plugin_hybrid",
             "electric": "electric", "flex_fuel": "flex"}
META_TRANSMISSION = {"automatic": "automatic", "manual": "manual", "cvt": "automatic"}
META_DRIVETRAIN = {"FWD": "FWD", "RWD": "RWD", "AWD": "AWD", "4WD": "4X4", "2WD": "2WD"}


class MetaVehicleCatalog(ListingChannel):
    """POST /{catalog_id}/items_batch (item_type=VEHICLE, CREATE/UPDATE/DELETE), asynchronous; status via
    GET /{catalog_id}/check_batch_request_status. Catalog items power PAID Automotive Inventory Ads (which can show
    in the Marketplace placement). They are not organic Marketplace listings: "confirmed" means accepted into the
    catalog. Live calls need DF_META_LIVE_PUBLISHING_ALLOWED=true AND a connection authorised for live publishing.
    Implemented against the documented API; not verified with a real catalog."""

    key = "meta_catalog"
    label = "Meta vehicle catalog (Automotive Inventory Ads)"
    mode = "api"
    DOCS = {
        "integration": "Meta Marketing API, Catalog Batch API (items_batch, item_type=VEHICLE).",
        "supported_operations": ["create/upsert item", "update item", "delete item", "check batch status"],
        "credentials": "System-user access token with catalog_management + business_management, read from the env var named "
                       "on the connection (DF_META_TOKEN_<NAME>); catalog_id and vdp_url_template in the connection config.",
        "approvals": "Meta App Review for catalog_management; Business Verification for Advanced Access; ads_management to run "
                     "ads (UNVERIFIED for AIA specifically).",
        "fields": ["vehicle_id", "title", "description", "make", "model", "year", "mileage", "price", "state_of_vehicle",
                   "image[]", "address", "body_style", "exterior_color", "url", "vin", "availability"],
        "media": "Up to 20 image URLs (Meta fetches them); description max 5,000 characters, no links or all caps.",
        "rate_limits": "Catalog Batch: about 8 + 8*log2(impressions+visits) calls/min per catalog; up to 5,000 requests per call.",
        "confirmation": "Asynchronous: submitted (batch handle) then published_confirmed when the batch finishes without item errors.",
        "update_delete": "UPDATE (upsert) and DELETE supported. Sold vehicles are deleted from the catalog.",
        "failure_recovery": "429/5xx/network retried with backoff (idempotent upsert by vehicle_id); other 4xx fail with Meta's message.",
        "cost": "No API fee documented; ads are paid from the dealership's ad budget.",
        "status": "Implemented against documented API; NOT verified live (no credentials/App Review).",
    }

    def _base(self, catalog_id: str) -> str:
        s = get_settings()
        return f"{s.meta_graph_base_url}/{s.meta_graph_api_version}/{catalog_id}"

    def build_payload(self, *, content, vehicle, facts, connection_config):
        v, loc = facts["vehicle"], facts["location"]
        lc = content.get(connection_config.get("language", "en")) or content.get("en") or {}
        tpl = connection_config.get("vdp_url_template")
        url = tpl.format(vin=v.get("vin") or "", stock_number=v.get("stock_number") or "", external_id=vehicle.external_id) if tpl else None
        data = {
            "vehicle_id": str(vehicle.id), "title": lc.get("title"), "description": (lc.get("description") or "")[:5000],
            "make": v.get("make"), "model": v.get("model"), "year": v.get("year"), "trim": v.get("trim"),
            "mileage": {"value": v.get("mileage"), "unit": "MI"} if v.get("mileage") is not None else None,
            "price": f"{facts['price_usd']} USD" if facts.get("price_usd") else None,
            "state_of_vehicle": STATE_OF_VEHICLE.get(v.get("condition") or "used", "used"),
            "image": [{"url": u} for u in facts["photos"][:20]],
            "address": {"addr1": loc.get("address"), "city": loc.get("city"), "region": loc.get("state"),
                        "postal_code": loc.get("postal"), "country": "US"},
            "body_style": (v.get("body_style") or "").upper().replace(" ", "_") or None,
            "exterior_color": v.get("exterior_color"), "transmission": META_TRANSMISSION.get(v.get("transmission") or ""),
            "drivetrain": META_DRIVETRAIN.get(v.get("drivetrain") or ""), "fuel_type": META_FUEL.get(v.get("fuel_type") or ""),
            "vin": v.get("vin"), "stock_number": v.get("stock_number"), "url": url, "availability": "available",
        }
        return {k: val for k, val in data.items() if val not in (None, "", [])}

    def validate_payload(self, payload):
        problems = [f"Meta requires '{k}'" for k in ("vehicle_id", "title", "description", "make", "model", "year", "mileage",
                                                      "price", "state_of_vehicle", "image", "address", "body_style",
                                                      "exterior_color", "url") if k not in payload]
        addr = payload.get("address", {})
        if addr and not all(addr.get(k) for k in ("addr1", "city", "region", "postal_code")):
            problems.append("Meta requires a full street address (vehicle location address/city/state/ZIP)")
        if "url" not in payload:
            problems.append("Configure vdp_url_template on the Meta connection (vehicle detail page URL)")
        return problems

    def _guard(self, connection) -> tuple[str, str]:
        if not get_settings().meta_live_publishing_allowed:
            raise PermanentError("meta: live publishing is disabled (DF_META_LIVE_PUBLISHING_ALLOWED is not true)")
        if connection is None or not connection.live_publishing:
            raise PermanentError("meta: this dealership's connection is not authorised for live publishing")
        catalog_id = (connection.config or {}).get("catalog_id")
        if not catalog_id:
            raise NotConfiguredError("meta: connection is missing catalog_id")
        token = os.environ.get(connection.token_env or "", "")
        if not token:
            raise NotConfiguredError(f"meta: environment variable {connection.token_env or '(none)'} is not set")
        return catalog_id, token

    def _batch(self, db, connection, requests: list[dict]) -> ChannelResult:
        catalog_id, token = self._guard(connection)
        with track_integration(db, "meta_catalog", "items_batch") as log:
            resp = request("POST", f"{self._base(catalog_id)}/items_batch", provider="meta_catalog", rate=(8, 60.0),
                           data={"access_token": token, "item_type": "VEHICLE", "allow_upsert": "true",
                                 "requests": json.dumps(requests)})
            body = resp.json()
            handles = body.get("handles") or []
            log["detail"] = f"{len(requests)} request(s), handle={handles[:1]}"
        if not handles:
            return ChannelResult(outcome="failed", error="Meta response had no batch handle", response=body)
        return ChannelResult(outcome="submitted", provider_ref=handles[0], response=body)

    def publish(self, db, payload, *, connection, idempotency_key):
        res = self._batch(db, connection, [{"method": "UPDATE", "data": payload}])
        res.external_id = payload["vehicle_id"]
        return res

    def delete(self, db, *, connection, external_id, idempotency_key, payload):
        return self._batch(db, connection, [{"method": "DELETE", "data": {"vehicle_id": external_id or payload.get("vehicle_id")}}])

    def check_status(self, db, *, connection, provider_ref, payload):
        catalog_id, token = self._guard(connection)
        with track_integration(db, "meta_catalog", "check_batch_request_status"):
            resp = request("GET", f"{self._base(catalog_id)}/check_batch_request_status", provider="meta_catalog",
                           params={"handle": provider_ref, "access_token": token})
        body = resp.json()
        entry = (body.get("data") or [{}])[0]
        status, errors = str(entry.get("status", "")).lower(), entry.get("errors") or []
        if status in ("finished", "complete", "completed") and not errors:
            return ChannelResult(outcome="confirmed", response=body)
        if errors:
            return ChannelResult(outcome="failed", error="; ".join(str(e.get("message", e)) for e in errors)[:1000], response=body)
        return ChannelResult(outcome="submitted", provider_ref=provider_ref, response=body)


# --- SIMULATED channel ------------------------------------------------------------------------------

class MockListingChannel(ListingChannel):
    """SIMULATED asynchronous API for tests and demos. Inject failures with ``fail_next`` (retryable) or
    ``fail_next_permanent``."""

    key = "mock"
    label = "Mock channel (SIMULATED, test only)"
    mode = "mock"
    simulated = True
    DOCS = {"integration": "SIMULATED. Never contacts a real platform.", "supported_operations": ["create", "update", "delete", "status"],
            "credentials": "None", "fields": ["title", "description", "price", "photos"], "media": "Photo URLs",
            "rate_limits": "None", "confirmation": "Asynchronous: submitted, then confirmed on status check.",
            "update_delete": "Supported", "failure_recovery": "Injectable retryable/permanent failures.", "status": "Test fixture"}
    fail_next = 0
    fail_next_permanent = 0
    calls: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.fail_next = cls.fail_next_permanent = 0
        cls.calls = []

    def _maybe_fail(self, db, action: str) -> None:
        with track_integration(db, "mock_listing_channel", action, simulated=True):
            MockListingChannel.calls.append(action)
            if MockListingChannel.fail_next_permanent:
                MockListingChannel.fail_next_permanent -= 1
                raise PermanentError("mock channel: simulated permanent rejection")
            if MockListingChannel.fail_next:
                MockListingChannel.fail_next -= 1
                raise IntegrationError("mock channel: simulated outage")

    def build_payload(self, *, content, vehicle, facts, connection_config):
        return {"id": str(vehicle.id), "title": content["en"]["title"], "description": content["en"]["description"],
                "price": facts.get("price_usd"), "photos": facts["photos"]}

    def publish(self, db, payload, *, connection, idempotency_key):
        self._maybe_fail(db, "publish")
        return ChannelResult(outcome="submitted", external_id=payload["id"], provider_ref=f"mock-{idempotency_key[-24:]}",
                             external_url=f"https://mock-channel.invalid/listings/{payload['id']}")

    def delete(self, db, *, connection, external_id, idempotency_key, payload):
        self._maybe_fail(db, "delete")
        return ChannelResult(outcome="confirmed")

    def check_status(self, db, *, connection, provider_ref, payload):
        self._maybe_fail(db, "status")
        return ChannelResult(outcome="confirmed")


CHANNELS: dict[str, ListingChannel] = {c.key: c for c in (FacebookMarketplaceManual(), MetaVehicleCatalog(), MockListingChannel())}


def get_channel(key: str) -> ListingChannel:
    if key not in CHANNELS:
        raise KeyError(key)
    return CHANNELS[key]


__all__ = ["CHANNELS", "ChannelResult", "CredentialError", "FacebookMarketplaceManual", "ListingChannel",
           "MARKETPLACE_URL_RE", "MetaVehicleCatalog", "MockListingChannel", "get_channel"]
