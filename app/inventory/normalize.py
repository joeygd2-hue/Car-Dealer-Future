"""Map heterogeneous dealer/DMS/feed records onto the canonical vehicle schema.

Mapping order: explicit ``column_map`` from the source config, then built-in
header aliases. Values are normalised conservatively: anything we cannot parse
with confidence becomes ``None`` plus a validation issue, never a guess.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.inventory.vin import normalize_vin

CANONICAL_FIELDS = [
    "external_id", "vin", "stock_number", "year", "make", "model", "trim", "body_style", "mileage", "price",
    "condition", "exterior_color", "interior_color", "transmission", "drivetrain", "fuel_type", "engine",
    "features", "photos", "location_name", "location_address", "location_city", "location_state", "location_postal",
    "description", "status", "title_status", "owner_count",
]

# Header aliases (compared case/space/punctuation-insensitively). These cover
# common spellings in dealer spreadsheets and DMS/syndication CSV exports; a
# vendor-specific layout should be captured as a column_map once we have a real
# sample file from that vendor.
ALIASES: dict[str, list[str]] = {
    "external_id": ["external_id", "id", "vehicle_id", "inventory_id", "listing_id", "unit_id", "dealer_vehicle_id"],
    "vin": ["vin", "vin_number", "vehicle_identification_number"],
    "stock_number": ["stock_number", "stock", "stock_no", "stock_num", "stocknumber", "stock#"],
    "year": ["year", "model_year", "yr"],
    "make": ["make", "manufacturer", "brand"],
    "model": ["model", "model_name"],
    "trim": ["trim", "trim_level", "series", "style"],
    "body_style": ["body_style", "body", "body_type", "bodystyle"],
    "mileage": ["mileage", "miles", "odometer", "odometer_reading", "odometer_miles", "km"],
    "price": ["price", "internet_price", "sale_price", "selling_price", "retail_price", "list_price", "asking_price", "retail"],
    "condition": ["condition", "type", "new_used", "vehicle_condition", "state_of_vehicle"],
    "exterior_color": ["exterior_color", "ext_color", "color", "extcolor", "exterior"],
    "interior_color": ["interior_color", "int_color", "intcolor", "interior"],
    "transmission": ["transmission", "trans", "transmission_type"],
    "drivetrain": ["drivetrain", "drive", "drive_type", "drivetrain_type", "drive_train"],
    "fuel_type": ["fuel_type", "fuel", "engine_fuel_type"],
    "engine": ["engine", "engine_description", "engine_desc", "motor"],
    "features": ["features", "options", "equipment", "feature_list", "installed_options"],
    "photos": ["photos", "photo_urls", "image_urls", "images", "imagelist", "image_list", "photo_url", "picture_urls", "image"],
    "location_name": ["location", "location_name", "lot", "dealer_name", "store"],
    "location_address": ["address", "location_address", "street"],
    "location_city": ["city", "location_city"],
    "location_state": ["state", "location_state"],
    "location_postal": ["zip", "zip_code", "postal_code", "location_postal", "zipcode"],
    "description": ["description", "comments", "dealer_comments", "notes", "seller_comments", "vehicle_description"],
    "status": ["status", "inventory_status", "availability", "stock_status"],
    "title_status": ["title_status", "title", "title_brand"],
    "owner_count": ["owners", "owner_count", "number_of_owners"],
}


UPPER_MAKES = {"bmw": "BMW", "gmc": "GMC", "ram": "RAM", "mini": "MINI", "fiat": "FIAT", "mg": "MG"}
SPECIAL_MAKES = {"mercedes-benz": "Mercedes-Benz", "land rover": "Land Rover", "alfa romeo": "Alfa Romeo",
                 "aston martin": "Aston Martin", "rolls-royce": "Rolls-Royce", "mclaren": "McLaren"}


def canonical_make(make: str) -> str:
    low = make.strip().lower()
    if low in UPPER_MAKES:
        return UPPER_MAKES[low]
    if low in SPECIAL_MAKES:
        return SPECIAL_MAKES[low]
    return make.title() if (make.islower() or make.isupper()) else make


def _key(h: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", h.lower())


_ALIAS_INDEX = {_key(a): field for field, names in ALIASES.items() for a in names}


def build_header_map(headers: list[str], column_map: dict[str, str] | None = None) -> dict[str, str]:
    """Return {source_header: canonical_field}."""
    mapping: dict[str, str] = {}
    explicit = {v: k for k, v in (column_map or {}).items() if v}
    taken: set[str] = set()
    for h in headers:
        if h in explicit:
            mapping[h] = explicit[h]
            taken.add(explicit[h])
    for h in headers:
        if h in mapping:
            continue
        f = _ALIAS_INDEX.get(_key(h))
        if f and f not in taken:
            mapping[h] = f
            taken.add(f)
    return mapping


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in {"n/a", "na", "null", "none", "-", "--"} else None


def parse_int(v: Any) -> int | None:
    s = _clean(v)
    if s is None:
        return None
    s = re.sub(r"(?i)\s*(mi|miles|km)\.?$", "", s).replace(",", "").strip()
    if re.fullmatch(r"\d+(\.0+)?", s):
        return int(float(s))
    return None


def parse_price_cents(v: Any) -> int | None:
    s = _clean(v)
    if s is None:
        return None
    s = s.replace("$", "").replace(",", "").replace("USD", "").strip()
    if re.fullmatch(r"\d+(\.\d{1,2})?", s):
        cents = round(float(s) * 100)
        return cents if cents > 0 else None
    return None


CONDITION_MAP = {"used": "used", "pre-owned": "used", "preowned": "used", "u": "used", "new": "new", "n": "new",
                 "cpo": "certified_pre_owned", "certified": "certified_pre_owned",
                 "certified pre-owned": "certified_pre_owned", "certified_pre_owned": "certified_pre_owned"}
TRANSMISSION_MAP = {"automatic": "automatic", "auto": "automatic", "a": "automatic", "manual": "manual", "m": "manual",
                    "standard": "manual", "stick": "manual", "cvt": "cvt", "dct": "automatic"}
DRIVETRAIN_MAP = {"fwd": "FWD", "front wheel drive": "FWD", "front-wheel drive": "FWD", "rwd": "RWD",
                  "rear wheel drive": "RWD", "rear-wheel drive": "RWD", "awd": "AWD", "all wheel drive": "AWD",
                  "all-wheel drive": "AWD", "4wd": "4WD", "4x4": "4WD", "four wheel drive": "4WD",
                  "four-wheel drive": "4WD", "2wd": "2WD", "4x2": "2WD"}
FUEL_MAP = {"gas": "gasoline", "gasoline": "gasoline", "petrol": "gasoline", "unleaded": "gasoline", "diesel": "diesel",
            "hybrid": "hybrid", "gas/electric hybrid": "hybrid", "plug-in hybrid": "plug_in_hybrid", "phev": "plug_in_hybrid",
            "electric": "electric", "ev": "electric", "bev": "electric", "flex": "flex_fuel", "flex fuel": "flex_fuel",
            "e85": "flex_fuel", "flex_fuel": "flex_fuel", "plug_in_hybrid": "plug_in_hybrid"}
STATUS_MAP = {"available": "available", "in stock": "available", "instock": "available", "active": "available",
              "for sale": "available", "pending": "pending", "on hold": "pending", "hold": "pending", "deposit": "pending",
              "sold": "sold", "delivered": "sold", "removed": "removed", "inactive": "removed", "deleted": "removed"}
TITLE_MAP = {"clean": "clean", "clear": "clean", "salvage": "salvage", "rebuilt": "rebuilt", "reconstructed": "rebuilt",
             "flood": "flood", "lemon": "lemon_buyback", "lemon buyback": "lemon_buyback"}


def _lookup(table: dict[str, str], v: Any, field: str, issues: list[dict]) -> str | None:
    s = _clean(v)
    if s is None:
        return None
    hit = table.get(s.lower())
    if hit is None:
        issues.append({"code": "unrecognized_value", "field": field, "severity": "warning",
                       "message": f"Unrecognised {field.replace('_', ' ')} '{s}' was not used"})
    return hit


def split_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        items = [str(x) for x in v]
    else:
        s = str(v)
        sep = "|" if "|" in s else ";" if ";" in s else "\n" if "\n" in s else ","
        items = s.split(sep)
    seen, out = set(), []
    for it in (i.strip() for i in items):
        if it and it.lower() not in seen:
            seen.add(it.lower())
            out.append(it)
    return out


def split_photos(v: Any) -> list[str]:
    if v is None:
        return []
    items = v if isinstance(v, list) else re.split(r"[|;,\s]+", str(v))
    out = []
    for u in (str(i).strip() for i in items):
        if u and u not in out:
            out.append(u)
    return out


def normalize_record(raw: dict[str, Any], header_map: dict[str, str]) -> tuple[dict[str, Any], list[dict]]:
    """Return (canonical_record, issues). The raw record is not modified."""
    issues: list[dict] = []
    src: dict[str, Any] = {}
    for h, f in header_map.items():
        if h in raw:
            src[f] = raw[h]

    rec: dict[str, Any] = {k: None for k in CANONICAL_FIELDS}
    for f in ("make", "model", "trim", "body_style", "exterior_color", "interior_color", "engine", "location_name",
              "location_address", "location_city", "location_state", "location_postal", "description", "stock_number"):
        rec[f] = _clean(src.get(f))
    rec["external_id"] = _clean(src.get("external_id"))
    rec["vin"] = normalize_vin(_clean(src.get("vin")))

    if _clean(src.get("year")) is not None:
        y = parse_int(src.get("year"))
        if y is None or not (1900 <= y <= date.today().year + 2):
            issues.append({"code": "invalid_year", "field": "year", "severity": "error",
                           "message": f"Year '{src.get('year')}' is not a valid model year"})
        else:
            rec["year"] = y
    if _clean(src.get("mileage")) is not None:
        m = parse_int(src.get("mileage"))
        if m is None:
            issues.append({"code": "unparseable_mileage", "field": "mileage", "severity": "warning",
                           "message": f"Mileage '{src.get('mileage')}' could not be read; it was left blank"})
        else:
            rec["mileage"] = m
    if _clean(src.get("price")) is not None:
        p = parse_price_cents(src.get("price"))
        if p is None:
            issues.append({"code": "unparseable_price", "field": "price", "severity": "warning",
                           "message": f"Price '{src.get('price')}' is not a number; it was left blank"})
        rec["price"] = p
    if _clean(src.get("owner_count")) is not None:
        rec["owner_count"] = parse_int(src.get("owner_count"))

    rec["condition"] = _lookup(CONDITION_MAP, src.get("condition"), "condition", issues)
    rec["transmission"] = _lookup(TRANSMISSION_MAP, src.get("transmission"), "transmission", issues)
    rec["drivetrain"] = _lookup(DRIVETRAIN_MAP, src.get("drivetrain"), "drivetrain", issues)
    rec["fuel_type"] = _lookup(FUEL_MAP, src.get("fuel_type"), "fuel_type", issues)
    rec["status"] = _lookup(STATUS_MAP, src.get("status"), "status", issues)
    rec["title_status"] = _lookup(TITLE_MAP, src.get("title_status"), "title_status", issues)
    rec["features"] = split_list(src.get("features"))
    rec["photos"] = split_photos(src.get("photos"))
    if rec["make"]:
        rec["make"] = canonical_make(rec["make"])
    if rec["model"] and rec["model"].islower():
        rec["model"] = rec["model"].title()
    if rec["location_state"]:
        rec["location_state"] = rec["location_state"].upper() if len(rec["location_state"]) == 2 else rec["location_state"]
    if raw.get("__extra_cells"):
        issues.append({"code": "extra_cells", "field": None, "severity": "warning",
                       "message": "Row has more cells than headers; extra cells were ignored (possible unquoted comma)"})
    return rec, issues
