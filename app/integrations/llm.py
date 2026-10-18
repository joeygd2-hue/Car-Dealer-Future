"""Optional Claude-backed rephrasing. The deterministic engine is always the source of facts;
LLM output is validated (app.services.responder.safety.validate_output) and discarded on any violation.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Estimated USD per 1M tokens for cost tracking (claude-opus-5-5 list price).
_PRICE_IN, _PRICE_OUT = 4.00, 20.00


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        import anthropic  # optional dependency: `uv sync --extra llm`

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=30.0)
        self.model = model

    def complete(self, *, system: str, prompt: str, max_tokens: int = 800) -> tuple[str | None, float]:
        a = self._anthropic
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (a.RateLimitError, a.APIConnectionError, a.APIStatusError) as exc:
            log.warning("LLM call failed: %s", exc)
            return None, 0.0
        usage = resp.usage
        cost_cents = ((usage.input_tokens * _PRICE_IN) + (usage.output_tokens * _PRICE_OUT)) / 1_000_000 * 100
        if resp.stop_reason == "refusal":
            return None, cost_cents
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return (text or None), cost_cents
