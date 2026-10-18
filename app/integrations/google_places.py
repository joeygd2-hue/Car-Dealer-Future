"""Google Places API (New) Text Search for prospect discovery.

Terms: only ``place_id`` may be stored indefinitely; other Places content is stored with a
30-day expiry and purged/refreshed (see docs/research). Places does not return email addresses.
"""

from __future__ import annotations

import httpx

from app.integrations.base import IntegrationError, ProspectCandidate

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.nationalPhoneNumber", "places.websiteUri", "places.types", "places.businessStatus",
    "places.googleMapsUri",
])


class GooglePlacesSource:
    name = "GOOGLE_PLACES"
    simulated = False
    # Text Search with contact fields bills at the Enterprise SKU (~$35 / 1,000 after free tier). Estimate.
    estimated_cost_cents_per_call = 3.5

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=20)

    def search(self, *, city: str, limit: int) -> list[ProspectCandidate]:
        resp = self.client.post(URL, headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELDS},
                                json={"textQuery": f"used car dealer in {city}, TX", "pageSize": min(limit, 20)})
        if resp.status_code != 200:
            raise IntegrationError(f"Places {resp.status_code}: {resp.text[:200]}",
                                   retryable=resp.status_code >= 500 or resp.status_code == 429)
        out = []
        for p in resp.json().get("places", []):
            comps = {t: c.get("shortText") for c in p.get("addressComponents", []) for t in c.get("types", [])}
            out.append(ProspectCandidate(
                name=p.get("displayName", {}).get("text", ""), source=self.name, source_ref=p.get("id"),
                address=p.get("formattedAddress"), city=comps.get("locality") or city,
                state=comps.get("administrative_area_level_1"), postal_code=comps.get("postal_code"),
                phone=p.get("nationalPhoneNumber"), website=p.get("websiteUri"), email=None,
                source_url=p.get("googleMapsUri"), categories=p.get("types", []),
                business_status=p.get("businessStatus"), content_ttl_days=30))
        return out
