"""Shared Claude client wrapper used by listing generation and the outreach agent.

- Disabled unless an API key is present (and the caller's feature flag is on).
- Uses structured outputs so responses are schema-validated JSON.
- Opts into server-side refusal fallbacks and checks ``stop_reason``.
- Records token usage so operating costs are *measured*, not estimated.
Prompts receive only the data needed for the task; secrets are never included.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from app.config import settings

T = TypeVar("T", bound=BaseModel)

# USD per million tokens for cost accounting (from the published price list;
# update when models or prices change).
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


class AIUnavailable(Exception):
    pass


class AIRefused(Exception):
    pass


@dataclass
class AIResult:
    parsed: Any
    model: str
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.model, PRICES["claude-opus-5-5"])
        return round(self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout, 6)


_client = None


def ai_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    global _client
    if not ai_configured():
        raise AIUnavailable("ANTHROPIC_API_KEY is not set")
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=120.0, max_retries=2)
    return _client


def structured_call(system: str, user: str, schema: type[T], *, max_tokens: int = 8000, effort: str | None = None) -> AIResult:
    import anthropic
    client = _get_client()
    try:
        resp = client.messages.parse(
            model=settings.ai_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort or settings.ai_effort},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
    except anthropic.RateLimitError as exc:
        raise RuntimeError(f"AI rate limited: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"AI request failed ({exc.status_code})") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError("AI connection failed") from exc
    if resp.stop_reason == "refusal":
        raise AIRefused("The model declined this request")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise RuntimeError("AI response was incomplete")
    return AIResult(parsed=resp.parsed_output, model=resp.model, input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens)
