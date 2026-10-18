"""Meta vehicle catalog (Automotive Inventory Ads) via the Catalog Batch API.

CONFIRMED (docs/research/meta-and-inventory-integrations-2026-10.md):
POST /{catalog_id}/items_batch with item_type=VEHICLE and CREATE/UPDATE/DELETE
requests; asynchronous - returns handles polled via
GET /{catalog_id}/check_batch_request_status. Needs catalog_management +
business_management (App Review; Business Verification for Advanced Access).

IMPORTANT: a catalog item powers *paid* Automotive Inventory Ads (which can
appear in the Marketplace placement). It is NOT an organic Marketplace listing.
"Confirmed" here means Meta accepted the item into the catalog.

Live calls require META_LIVE_PUBLISHING_ALLOWED=true AND a dealership
connection explicitly authorised for live publishing. Not yet verified against
a real catalog (no credentials available during Phase 2).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings
from app.core.crypto import decrypt_secret
from app.publishing.adapters.base import AdapterError, AdapterResult, ChannelAdapter

STATE_OF_VEHICLE = {"used": "used", "new": "new", "certified_pre_owned": "cpo"}
FUEL = {"gasoline": "gasoline", "diesel": "diesel", "hybrid": "hybrid", "plug_in_hybrid": "plugin_hybrid",
        "electric": "electric", "flex_fuel": "flex"}
TRANSMISSION = {"automatic": "automatic", "manual": "manual", "cvt": "automatic"}
DRIVETRAIN = {"FWD": "FWD", "RWD": "RWD", "AWD": "AWD", "4WD": "4X4", "2WD": "2WD"}

_transport: httpx.BaseTransport | None = None


def set_transport(t: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = t


class MetaCatalogAdapter(ChannelAdapter):
    key = "meta_catalog"
    label = "Meta vehicle catalog (Automotive Inventory Ads)"
    mode = "api"
    requires_connection = True
    DOCS = {
        "integration": "Meta Marketing API - Catalog Batch API (items_batch, item_type=VEHICLE).",
        "supported_operations": ["create/upsert item", "update item", "delete item", "check batch status"],
        "credentials": "System-user access token for the dealer's Business Manager with catalog_management + "
                       "business_management; catalog_id. Stored encrypted (APP_ENCRYPTION_KEY).",
        "approvals": "Meta App Review for catalog_management; Business Verification for Advanced Access; ads_management "
                     "to run ads (UNVERIFIED for AIA specifically).",
        "fields": ["vehicle_id", "title", "description", "make", "model", "year", "mileage{value,unit}", "price",
                   "state_of_vehicle", "image[]", "address", "body_style", "exterior_color", "url", "vin", "availability",
                   "transmission", "drivetrain", "fuel_type", "trim", "stock_number"],
        "media": "Up to 20 image URLs (Meta fetches them); description max 5,000 chars, no links/all caps.",
        "rate_limits": "Catalog Batch: ~8 + 8*log2(impressions+visits) calls/min per catalog; up to 5,000 requests/call.",
        "confirmation": "Asynchronous: 'submitted' with a batch handle, then 'published_confirmed' (accepted into catalog) "
                        "after check_batch_request_status reports finished without item errors.",
        "update_delete": "UPDATE (upsert) and DELETE supported. Sold vehicles are deleted from the catalog.",
        "failure_recovery": "HTTP 429/5xx/network -> retried with backoff (idempotent upsert by vehicle_id); 4xx -> failed "
                            "with Meta's error message; item-level errors -> failed.",
        "cost": "No API fee documented; ads are paid by the dealership's ad budget.",
        "status": "Implemented against documented API; NOT verified live (no credentials/App Review).",
    }

    def _base(self, catalog_id: str) -> str:
        return f"{settings.meta_graph_base_url}/{settings.meta_graph_api_version}/{catalog_id}"

    def build_payload(self, *, content: dict, vehicle, facts: dict, connection_config: dict) -> dict[str, Any]:
        v, loc = facts["vehicle"], facts["location"]
        lang = connection_config.get("language", "en")
        lc = content.get(lang) or content.get("en") or {}
        url_tpl = connection_config.get("vdp_url_template")
        url = url_tpl.format(vin=v.get("vin") or "", stock_number=v.get("stock_number") or "",
                             external_id=vehicle.external_id) if url_tpl else None
        data = {
            "vehicle_id": vehicle.id,
            "title": lc.get("title"),
            "description": lc.get("description", "")[:5000],
            "make": v.get("make"), "model": v.get("model"), "year": v.get("year"), "trim": v.get("trim"),
            "mileage": {"value": v.get("mileage"), "unit": "MI"} if v.get("mileage") is not None else None,
            "price": f"{facts['price_usd']} USD" if facts.get("price_usd") else None,
            "state_of_vehicle": STATE_OF_VEHICLE.get(v.get("condition") or "used", "used"),
            "image": [{"url": u} for u in facts["photos"][:20]],
            "address": {"addr1": loc.get("address"), "city": loc.get("city"), "region": loc.get("state"),
                        "postal_code": loc.get("postal"), "country": "US"},
            "body_style": (v.get("body_style") or "").upper().replace(" ", "_") or None,
            "exterior_color": v.get("exterior_color"),
            "transmission": TRANSMISSION.get(v.get("transmission") or ""),
            "drivetrain": DRIVETRAIN.get(v.get("drivetrain") or ""),
            "fuel_type": FUEL.get(v.get("fuel_type") or ""),
            "vin": v.get("vin"), "stock_number": v.get("stock_number"),
            "url": url,
            "availability": "available",
        }
        return {k: val for k, val in data.items() if val not in (None, "", [])}

    def validate_payload(self, payload: dict[str, Any]) -> list[str]:
        problems = [f"Meta requires '{k}'" for k in ("vehicle_id", "title", "description", "make", "model", "year", "mileage",
                                                      "price", "state_of_vehicle", "image", "address", "body_style",
                                                      "exterior_color", "url") if k not in payload]
        addr = payload.get("address", {})
        if addr and not all(addr.get(k) for k in ("addr1", "city", "region", "postal_code")):
            problems.append("Meta requires a full street address (vehicle location address/city/state/ZIP)")
        if "url" not in payload:
            problems.append("Configure vdp_url_template on the Meta connection (vehicle detail page URL)")
        return problems

    def _token(self, connection) -> str:
        if not connection or not connection.secret_ciphertext:
            raise AdapterError("Meta connection has no access token configured")
        return decrypt_secret(connection.secret_ciphertext)

    def _guard_live(self, connection) -> None:
        if not settings.meta_live_publishing_allowed:
            raise AdapterError("Live Meta publishing is disabled (META_LIVE_PUBLISHING_ALLOWED is not true)")
        if not connection or not connection.live_publishing:
            raise AdapterError("This dealership's Meta connection is not authorised for live publishing")

    def _post_batch(self, connection, requests: list[dict]) -> AdapterResult:
        self._guard_live(connection)
        catalog_id = (connection.config or {}).get("catalog_id")
        if not catalog_id:
            raise AdapterError("Meta connection is missing catalog_id")
        form = {"access_token": self._token(connection), "item_type": "VEHICLE", "allow_upsert": "true",
                "requests": json.dumps(requests)}
        try:
            with httpx.Client(timeout=30, transport=_transport) as client:
                resp = client.post(f"{self._base(catalog_id)}/items_batch", data=form)
        except httpx.HTTPError as exc:
            raise AdapterError(f"Network error calling Meta: {type(exc).__name__}", retryable=True) from exc
        body = _json(resp)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise AdapterError(f"Meta returned HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            msg = (body.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            return AdapterResult(outcome="failed", error=f"Meta rejected the request: {msg}", response=_redact(body))
        handles = body.get("handles") or []
        if not handles:
            return AdapterResult(outcome="failed", error="Meta response had no batch handle", response=_redact(body))
        return AdapterResult(outcome="submitted", provider_ref=handles[0], response=_redact(body))

    def publish(self, payload: dict, *, connection, idempotency_key: str) -> AdapterResult:
        res = self._post_batch(connection, [{"method": "UPDATE", "data": payload}])
        res.external_id = payload["vehicle_id"]
        return res

    def update(self, payload: dict, *, connection, idempotency_key: str, external_id: str | None) -> AdapterResult:
        return self.publish(payload, connection=connection, idempotency_key=idempotency_key)

    def delete(self, *, connection, external_id: str | None, idempotency_key: str, payload: dict) -> AdapterResult:
        return self._post_batch(connection, [{"method": "DELETE", "data": {"vehicle_id": external_id or payload.get("vehicle_id")}}])

    def check_status(self, *, connection, provider_ref: str, payload: dict) -> AdapterResult:
        self._guard_live(connection)
        catalog_id = (connection.config or {}).get("catalog_id")
        try:
            with httpx.Client(timeout=30, transport=_transport) as client:
                resp = client.get(f"{self._base(catalog_id)}/check_batch_request_status",
                                  params={"handle": provider_ref, "access_token": self._token(connection)})
        except httpx.HTTPError as exc:
            raise AdapterError(f"Network error calling Meta: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise AdapterError(f"Meta returned HTTP {resp.status_code}", retryable=True)
        body = _json(resp)
        if resp.status_code >= 400:
            return AdapterResult(outcome="failed", error=(body.get("error") or {}).get("message", "status check failed"))
        entry = (body.get("data") or [{}])[0]
        status = str(entry.get("status", "")).lower()
        errors = entry.get("errors") or []
        if status in ("finished", "complete", "completed") and not errors:
            return AdapterResult(outcome="confirmed", response=_redact(body))
        if errors:
            return AdapterResult(outcome="failed", error="; ".join(str(e.get("message", e)) for e in errors)[:1000],
                                 response=_redact(body))
        return AdapterResult(outcome="submitted", provider_ref=provider_ref, response=_redact(body))


def _json(resp) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def _redact(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "access_token"}
