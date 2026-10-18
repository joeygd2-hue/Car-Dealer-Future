from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def fingerprint(value: Any) -> str:
    """Stable hash of JSON-like data (key order does not matter)."""
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))
