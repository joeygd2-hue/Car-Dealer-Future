"""Thin gateway for Claude API calls that records measured token usage.

Token counts come from the API response (`usage`) and are therefore measured.
Cost is an *estimate* computed from the `provider_rates` table, because the
Messages API response does not include billed cost. Verified cost requires
importing provider billing data (see services/costs.import_verified_costs).

AI is optional: when disabled or unconfigured, callers fall back to
deterministic templates and rules.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services import costs

log = logging.getLogger(__name__)


class AIUnavailable(Exception):
    pass


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    refused: bool = False


def ai_available() -> bool:
    s = get_settings()
    return bool(s.ai_enabled and s.anthropic_api_key)


def complete(
    db: Session,
    *,
    system: str,
    prompt: str,
    feature: str,
    client_id: int | None = None,
    max_tokens: int = 2000,
    effort: str = "low",
) -> AIResult:
    """Run one Messages API request and record its usage. Raises AIUnavailable if AI is off."""
    if not ai_available():
        raise AIUnavailable("AI is not enabled (set AI_ENABLED=true and ANTHROPIC_API_KEY)")
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=2, timeout=120)
    try:
        response = client.beta.messages.create(
            model=s.ai_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIConnectionError as exc:
        raise AIUnavailable(f"AI provider unreachable: {exc}") from exc
    except anthropic.RateLimitError as exc:
        raise AIUnavailable("AI provider rate limited the request") from exc
    except anthropic.APIStatusError as exc:
        raise AIUnavailable(f"AI provider error {exc.status_code}") from exc

    usage = response.usage
    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    model = response.model
    total_cost = 0
    any_unknown = False
    for metric, qty in (
        ("input_tokens", in_tok),
        ("output_tokens", out_tok),
        ("cache_write_tokens", cache_write),
        ("cache_read_tokens", cache_read),
    ):
        if qty == 0:
            continue
        rec = costs.record_usage(
            db,
            provider=f"anthropic:{model}",
            service_category="ai",
            metric=metric,
            quantity=qty,
            client_id=client_id,
            feature=feature,
            idempotency_key=f"anthropic:{response.id}:{metric}",
        )
        if rec.cost_micros is None:
            any_unknown = True
        else:
            total_cost += rec.cost_micros

    refused = response.stop_reason == "refusal"
    text = "" if refused else "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    return AIResult(
        text=text,
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_micros=None if any_unknown else total_cost,
        refused=refused,
    )
