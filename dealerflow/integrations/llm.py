"""Optional Claude API gateway that records measured token usage as cost entries.

Token counts come from the API response (``usage``) and are measured. Cost is an *estimate*
(tokens x the per-token rate in the ``cost_estimates`` setting) because the Messages API does not
return billed cost; verified cost comes only from importing provider billing data.

AI is off by default (DF_AI_ENABLED=false). Callers must fall back to deterministic templates/rules
when ``AIUnavailable`` is raised, and AI-written customer/prospect-facing text always needs approval.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..audit import track_integration
from ..config import get_settings
from ..services import usage


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
