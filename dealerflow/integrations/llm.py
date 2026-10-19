"""The one Claude (Anthropic) gateway for the app.

Two entry points:

* ``structured_call`` (Phase 2 listing generator): schema-validated output; cost computed from ``PRICES``.
  Callers check their own feature flag (``DF_LISTING_AI_ENABLED``).
* ``complete`` (Phase 4): free text; measured token usage is recorded as estimated cost entries
  (tokens x the per-token rate in the ``cost_estimates`` setting), because the Messages API does not
  return billed cost. Off unless ``DF_AI_ENABLED=true``; callers fall back to deterministic templates/rules
  when ``AIUnavailable`` is raised.

Disabled unless DF_ANTHROPIC_API_KEY is set. Prompts carry only the data the task needs; the key is read
from settings and never logged. Model output is untrusted: callers validate it (see
services/listings/guardrails.py), and AI-written customer/prospect-facing text always needs approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..audit import track_integration
from ..config import get_settings
from ..services import usage
from .errors import IntegrationError, NotConfiguredError, PermanentError, RateLimitedError

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output), from the published price list; update with the model.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


class Refused(PermanentError):
    pass


@dataclass
class LLMResult:
    parsed: Any
    model: str
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.model, PRICES["claude-opus-5-5"])
        return round(self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout, 6)


_client = None


def _get_client():
    global _client
    key = get_settings().anthropic_api_key.get_secret_value()
    if not key:
        raise NotConfiguredError("DF_ANTHROPIC_API_KEY is not set")
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=2)
    return _client


def structured_call(system: str, user: str, schema: type[T], *, max_tokens: int = 8000, effort: str | None = None) -> LLMResult:
    import anthropic

    s = get_settings()
    try:
        resp = _get_client().messages.parse(
            model=s.ai_model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
            output_format=schema, output_config={"effort": effort or s.ai_effort},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"}, extra_body={"fallbacks": "default"},
        )
    except anthropic.RateLimitError as exc:
        raise RateLimitedError("anthropic: rate limited") from exc
    except anthropic.APIStatusError as exc:
        raise (IntegrationError if exc.status_code >= 500 else PermanentError)(f"anthropic: HTTP {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise IntegrationError("anthropic: connection failed") from exc
    if resp.stop_reason == "refusal":
        raise Refused("anthropic: the model declined this request")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise IntegrationError("anthropic: incomplete response")
    return LLMResult(parsed=resp.parsed_output, model=resp.model, input_tokens=resp.usage.input_tokens,
                     output_tokens=resp.usage.output_tokens)


class AIUnavailable(Exception):
    pass


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    refused: bool = False


def ai_available() -> bool:
    s = get_settings()
    return bool(s.ai_enabled and s.anthropic_api_key.get_secret_value())


def complete(db: Session, *, system: str, prompt: str, feature: str, client_id: int | None = None,
             max_tokens: int = 2000, effort: str = "low") -> AIResult:
    if not ai_available():
        raise AIUnavailable("AI is not enabled (set DF_AI_ENABLED=true and DF_ANTHROPIC_API_KEY)")
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key.get_secret_value(), max_retries=2, timeout=120)
    try:
        with track_integration(db, "anthropic", feature):
            response = client.beta.messages.create(
                model=s.ai_model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
    except anthropic.RateLimitError as exc:
        raise AIUnavailable("AI provider rate limited the request") from exc
    except anthropic.APIStatusError as exc:
        raise AIUnavailable(f"AI provider error {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIUnavailable("AI provider unreachable") from exc

    u = response.usage
    counts = {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
    }
    cost = 0.0
    for metric, qty in counts.items():
        if qty:
            row = usage.record_usage(db, provider=f"anthropic:{response.model}", category="ai", metric=metric, quantity=qty,
                                     client_id=client_id, ref=f"anthropic:{response.id}:{metric}", note=feature)
            cost += float(row.amount or 0)
    refused = response.stop_reason == "refusal"
    text = "" if refused else "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    return AIResult(text=text, model=response.model, input_tokens=counts["input_tokens"], output_tokens=counts["output_tokens"],
                    estimated_cost=round(cost, 6), refused=refused)