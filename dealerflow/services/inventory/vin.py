"""VIN structure checks (ISO 3779 / 49 CFR 565). Offline; no external lookup."""
from __future__ import annotations

import re

_TRANSLIT = {**{str(d): d for d in range(10)},
             **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))), "P": 7, "R": 9,
             **dict(zip("STUVWXYZ", range(2, 10)))}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"  # 30-year cycle starting 1980 / 2010
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalize_vin(v: str | None) -> str | None:
    if not v:
        return None
    s = re.sub(r"[\s-]", "", str(v)).upper()
    return s or None


def check_digit(vin: str) -> str:
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS))
    r = total % 11
    return "X" if r == 10 else str(r)


def vin_problems(vin: str) -> list[str]:
    if len(vin) != 17:
        return [f"VIN must be 17 characters (got {len(vin)})"]
    if not VIN_RE.match(vin):
        return ["VIN contains invalid characters (I, O and Q are not allowed)"]
    if check_digit(vin) != vin[8]:
        # North-American VINs must pass; some imports do not use the check digit.
        return ["VIN check digit does not match (possible typo)"]
    return []


def model_years_for(vin: str) -> list[int]:
    """Candidate model years from position 10 (two 30-year cycles)."""
    if len(vin) != 17 or vin[9] not in _YEAR_CODES:
        return []
    idx = _YEAR_CODES.index(vin[9])
    return [1980 + idx, 2010 + idx]
