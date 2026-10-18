"""Lead discovery sources. Each returns ``LeadRecord``s with source URLs; none invents data.

- ``google_places``: Places API (New) Text Search with a field mask. Google's terms restrict
  caching of most Places content (place IDs may be stored). See docs/research/feasibility-report.md;
  the stored copy is refreshed or removed per those terms (``services.discovery.purge_stale_places``).
- ``osm``: OpenStreetMap via the Overpass API (ODbL, attribution required), shop=car.
- ``fixture``: SIMULATED synthetic dealerships from dealerflow/fixtures for local use and tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import get_settings
from ..services.prospects import LeadRecord
from .errors import NotConfiguredError
from .http import request

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_dealers.json"

PLACES_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents", "places.location",
    "places.nationalPhoneNumber", "places.websiteUri", "places.types", "places.businessStatus", "places.googleMapsUri",
    "nextPageToken",
])


def _component(comps: list[dict], kind: str, short: bool = False) -> str | None:
    for c in comps or []:
        if kind in c.get("types", []):
            return c.get("shortText" if short else "longText")
    return None


def google_places_search(query: str, *, max_pages: int = 1) -> list[LeadRecord]:
    s = get_settings()
    key = s.google_places_api_key.get_secret_value()
    if not key:
        raise NotConfiguredError("DF_GOOGLE_PLACES_API_KEY is not set")
    out: list[LeadRecord] = []
    page_token = None
    for _ in range(max_pages):
        body: dict = {"textQuery": query, "pageSize": 20,
                      "locationBias": {"circle": {"center": {"latitude": s.target_lat, "longitude": s.target_lng},
                                                  "radius": min(50000.0, s.target_radius_miles * 1609.34)}}}
        if page_token:
            body["pageToken"] = page_token
        r = request("POST", "https://places.googleapis.com/v1/places:searchText", provider="google_places",
                    headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELDS}, json=body, rate=(5, 1.0))
        data = r.json()
        for pl in data.get("places", []):
            if pl.get("businessStatus") and pl["businessStatus"] != "OPERATIONAL":
                continue
            comps = pl.get("addressComponents", [])
            num, street = _component(comps, "street_number"), _component(comps, "route")
            types = pl.get("types", [])
            out.append(LeadRecord(
                name=pl.get("displayName", {}).get("text", ""), source="google_places", external_id=pl["id"],
                source_url=pl.get("googleMapsUri"), website=pl.get("websiteUri"),
                address_line=" ".join(x for x in (num, street) if x) or None,
                city=_component(comps, "locality"), state=_component(comps, "administrative_area_level_1", True),
                postal_code=_component(comps, "postal_code"),
                lat=pl.get("location", {}).get("latitude"), lng=pl.get("location", {}).get("longitude"),
                phone=pl.get("nationalPhoneNumber"),
                dealership_type=None,  # Places types cannot tell independent from franchise; leave unknown
                raw={"types": types, "query": query},
            ))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


def osm_search(*, radius_miles: float | None = None) -> list[LeadRecord]:
    s = get_settings()
    radius_m = int((radius_miles or s.target_radius_miles) * 1609.34)
    q = (f'[out:json][timeout:60];(node["shop"="car"](around:{radius_m},{s.target_lat},{s.target_lng});'
         f'way["shop"="car"](around:{radius_m},{s.target_lat},{s.target_lng}););out center tags;')
    r = request("POST", s.overpass_url, provider="osm_overpass", data={"data": q}, rate=(1, 5.0), timeout=90)
    out = []
    for el in r.json().get("elements", []):
        t = el.get("tags", {})
        name = t.get("name")
        if not name:
            continue
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lng = el.get("lon") or el.get("center", {}).get("lon")
        used = (t.get("second_hand") in ("only", "yes")) or ("used" in name.lower())
        brand = t.get("brand") or t.get("operator:brand")
        dtype = "independent_used" if used and not brand else ("franchise" if brand and not used else None)
        out.append(LeadRecord(
            name=name, source="osm", external_id=f"{el['type']}/{el['id']}",
            source_url=f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
            website=t.get("website") or t.get("contact:website"),
            address_line=" ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x) or None,
            city=t.get("addr:city"), state=t.get("addr:state"), postal_code=t.get("addr:postcode"),
            lat=lat, lng=lng, phone=t.get("phone") or t.get("contact:phone"),
            email=t.get("email") or t.get("contact:email"), dealership_type=dtype,
            notes="Data © OpenStreetMap contributors (ODbL).", raw={"tags": t},
        ))
    return out


def fixture_search() -> list[LeadRecord]:
    """SIMULATED synthetic dealerships (fictional names, reserved 555 numbers, .invalid / example domains)."""
    rows = json.loads(FIXTURE_PATH.read_text())
    out = []
    for row in rows:
        signals = [tuple(x) for x in row.pop("signals", [])]
        out.append(LeadRecord(source="fixture", simulated=True, signals=signals, **row))
    return out


SOURCES = {"google_places": google_places_search, "osm": osm_search, "fixture": fixture_search}
