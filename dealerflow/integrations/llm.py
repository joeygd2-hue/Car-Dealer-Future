"""Optional Claude (Anthropic) API gateway shared by every phase.

AI is off by default (DF_AI_ENABLED=false). Callers fall back to deterministic templates/rules when it is
unavailable, and AI-written customer/prospect-facing text always needs human approval. The key is read
from settings and never logged. Model output is untrusted: callers validate it (for example
services/listings/guardrails.py).

Token counts come from the API response (``usage``) and are measured. Cost is an *estimate* because the
Messages API does not return billed cost; verified cost comes only from importing provider billing data.
When a ``db`` session is given, usage is recorded through ``services.usage`` (``cost_entries`` with
category ``ai`` and the client it was spent on).

Two entry points:

* ``complete`` – free-text completion (Phase 4 drafting). Raises ``AIUnavailable`` on any provider problem.
* ``structured_call`` – schema-validated output (Phase 2 listing prose). Raises the shared integration
  errors so the job queue can retry transient failures; a refusal raises ``Refused`` (permanent).
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..audit import track_integration
from ..config import get_settings
from .errors import IntegrationError, NotConfiguredError, PermanentError, RateLimitedError

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output), from the published price list; used only for the estimate shown
# with a listing revision. Update with the model.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}
_BETA = "server-side-fallback-2026-07-01"


class AIUnavailable(Exception):
    pass


class Refused(PermanentError):
    pass


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    refused: bool = False


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


def ai_available() -> bool:
    return get_settings().ai_available


def _client():
    import anthropic

    key = get_settings().anthropic_api_key.get_secret_value()
    if not key:
        raise NotConfiguredError("DF_ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=key, max_retries=2, timeout=120)


def _record_usage(db: Session | None, response: Any, feature: str, client_id: int | None) -> tuple[dict, float]:
    u = response.usage
    counts = {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
    }
    cost = 0.0
    if db is not None:
        from ..services import usage

        for metric, qty in counts.items():
            if qty:
                row = usage.record_usage(db, provider=f"anthropic:{response.model}", category="ai", metric=metric,
                                         quantity=qty, client_id=client_id, ref=f"anthropic:{response.id}:{metric}",
                                         note=feature)
                cost += float(row.amount or 0)
    return counts, cost


def complete(db: Session, *, system: str, prompt: str, feature: str, client_id: int | None = None,
             max_tokens: int = 2000, effort: str = "low") -> AIResult:
    if not ai_available():
        raise AIUnavailable("AI is not enabled (set DF_AI_ENABLED=true and DF_ANTHROPIC_API_KEY)")
    import anthropic

    s = get_settings()
    try:
        with track_integration(db, "anthropic", feature):
            response = _client().beta.messages.create(
                model=s.ai_model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort},
                betas=[_BETA], fallbacks="default",
            )
    except anthropic.RateLimitError as exc:
        raise AIUnavailable("AI provider rate limited the request") from exc
    except anthropic.APIStatusError as exc:
        raise AIUnavailable(f"AI provider error {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIUnavailable("AI provider unreachable") from exc

    counts, cost = _record_usage(db, response, feature, client_id)
    refused = response.stop_reason == "refusal"
    text = "" if refused else "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    return AIResult(text=text, model=response.model, input_tokens=counts["input_tokens"], output_tokens=counts["output_tokens"],
                    estimated_cost=round(cost, 6), refused=refused)


def structured_call(system: str, user: str, schema: type[T], *, max_tokens: int = 8000, effort: str | None = None,
                    db: Session | None = None, feature: str = "structured", client_id: int | None = None) -> LLMResult:
    if not ai_available():
        raise NotConfiguredError("AI is not enabled (set DF_AI_ENABLED=true and DF_ANTHROPIC_API_KEY)")
    import anthropic

    s = get_settings()
    gate = track_integration(db, "anthropic", feature) if db is not None else nullcontext()
    try:
        with gate:  # emergency stop and disabled-provider checks, plus the integration log
            resp = _client().messages.parse(
                model=s.ai_model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
                output_format=schema, output_config={"effort": effort or s.ai_effort},
                extra_headers={"anthropic-beta": _BETA}, extra_body={"fallbacks": "default"},
            )
    except anthropic.RateLimitError as exc:
        raise RateLimitedError("anthropic: rate limited") from exc
    except anthropic.APIStatusError as exc:
        raise (IntegrationError if exc.status_code >= 500 else PermanentError)(f"anthropic: HTTP {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise IntegrationError("anthropic: connection failed") from exc
    _record_usage(db, resp, feature, client_id)
    if resp.stop_reason == "refusal":
        raise Refused("anthropic: the model declined this request")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise IntegrationError("anthropic: incomplete response")
    return LLMResult(parsed=resp.parsed_output, model=resp.model, input_tokens=resp.usage.input_tokens,
                     output_tokens=resp.usage.output_tokens)
