"""Claude (Anthropic) structured-output calls with measured cost.

Disabled unless DF_ANTHROPIC_API_KEY is set (callers also check their own feature flag). Prompts carry
only the data the task needs; the key is read from settings and never logged. Model output is untrusted:
callers validate it (see services/listings/guardrails.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from ..config import get_settings
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
