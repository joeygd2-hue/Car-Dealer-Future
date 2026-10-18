"""Editable, non-secret settings stored in the database (with code defaults)."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.orm import Session

from ..audit import audit
from ..models import AppSetting

DEFAULTS: dict[str, Any] = {
    "scoring_weights": {
        "target_market_fit": 25,
        "geographic_fit": 20,
        "inventory_activity": 15,
        "web_presence": 10,
        "contact_available": 15,
        "bilingual_opportunity": 10,
        "engagement": 5,
    },
    "qualification_threshold": 55,
    # Estimated per-call costs (USD) used for budget checks; edit to match your provider invoices.
    "cost_estimates": {"google_places_text_search": 0.032, "email_send": 0.0, "zoom_meeting": 0.0, "twilio_call_minute": 0.014,
                       # Claude API list prices per token (first-party API; cached reference 2026-10-06 — re-verify
                       # at https://www.anthropic.com/pricing). Used only for *estimates*.
                       "anthropic:claude-opus-5-5:input_tokens": 0.000004, "anthropic:claude-opus-5-5:output_tokens": 0.00002,
                       "anthropic:claude-opus-5-5:cache_read_tokens": 0.0000002, "anthropic:claude-opus-5-5:cache_write_tokens": 0.000005,
                       "anthropic:claude-sonnet-5-5:input_tokens": 0.000002, "anthropic:claude-sonnet-5-5:output_tokens": 0.00001,
                       "anthropic:claude-haiku-5-5:input_tokens": 0.0000001, "anthropic:claude-haiku-5-5:output_tokens": 0.0000005,
                       "google:pagespeed:requests": 0.0},
}


def get_setting(db: Session, key: str) -> Any:
    row = db.get(AppSetting, key)
    if row is None:
        return copy.deepcopy(DEFAULTS.get(key))
    if isinstance(DEFAULTS.get(key), dict) and isinstance(row.value, dict):
        merged = copy.deepcopy(DEFAULTS[key])
        merged.update(row.value)
        return merged
    return row.value


def put_setting(db: Session, key: str, value: Any, *, actor_id: int | None) -> None:
    row = db.get(AppSetting, key)
    old = row.value if row else None
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_by_id=actor_id))
    else:
        row.value, row.updated_by_id = value, actor_id
    audit(db, "setting.update", actor_id=actor_id, entity_type="setting", data={"key": key, "old": old, "new": value})
