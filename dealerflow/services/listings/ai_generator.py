"""Claude-written listing prose, constrained to verified facts.

The model writes only titles, descriptions, calls to action and search terms.
Feature lists, FAQ answers and disclaimers stay deterministic (template
generator). Every result then passes through the same guardrails as any other
revision; nothing the model writes is trusted.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ...integrations.llm import structured_call
from . import template_generator
from .dealer_settings import ListingSettings


class LangCopy(BaseModel):
    title: str
    description: str
    call_to_action: str
    search_terms: list[str] = Field(default_factory=list)


class ListingCopy(BaseModel):
    en: LangCopy
    es: LangCopy
    uncertainty: list[str] = Field(default_factory=list)


SYSTEM = """You write used-vehicle listings for an independent dealership, in English and natural US Spanish.
Rules (all mandatory):
- Use ONLY the facts in the provided JSON. Never add features, options, warranty, accident or ownership history,
  inspection, certification, financing, payment, availability or condition claims that are not explicitly present.
- If a fact is missing, leave it out and add a short note to `uncertainty`. Never guess.
- Numbers (year, mileage, price) must be copied exactly. Do not round or restate them differently.
- No urgency or pressure language (e.g. "won't last", "act now", "hurry", "no te lo pierdas"). No superlatives about price.
- No keyword stuffing: mention the make/model naturally, at most a few times. 4-8 natural search terms per language.
- Title: include year, make, model and trim if present; no emojis, no all caps; at most {title_limit} characters.
- Description: brand voice "{voice}". {voice_notes} Plain text, short paragraphs, no links, at most {desc_limit} characters.
  Do not include the feature list, FAQ or disclaimers; they are appended separately.
- Spanish must be a natural rendering for Spanish-speaking buyers in Texas, not a word-for-word translation.
- Call to action: invite questions or a test drive using only the dealership contact details provided."""


def generate(facts: dict[str, Any], s: ListingSettings, feedback: str | None = None) -> tuple[dict[str, Any], list[str], dict]:
    base, uncertainty = template_generator.generate(facts, s)
    lim = s.character_limits.get("facebook_marketplace") or s.character_limits["default"]
    system = SYSTEM.format(title_limit=lim.title, desc_limit=min(lim.description, 3000), voice=s.brand_voice,
                           voice_notes=s.brand_voice_notes or "")
    prompt_facts = {k: v for k, v in facts.items() if k not in ("photos",)}
    prompt_facts["dealer"] = {k: v for k, v in facts["dealer"].items() if k in ("contact", "business_hours", "call_to_action", "brand_voice")}
    user = "Vehicle facts (verified):\n" + json.dumps(prompt_facts, ensure_ascii=False, indent=1)
    if feedback:
        user += "\n\nReviewer feedback on the previous draft (apply it only if it does not conflict with the rules):\n" + feedback[:2000]
    result = structured_call(system, user, ListingCopy, max_tokens=8000)
    copy: ListingCopy = result.parsed
    content = dict(base)
    for lang in s.languages:
        lc = getattr(copy, lang)
        desc = lc.description.strip()
        feats = base[lang]["features"]
        head = "Features listed by the dealership:" if lang == "en" else "Equipamiento indicado por el concesionario:"
        parts = [desc]
        if feats:
            parts.append(head + "\n" + "\n".join(f"• {x}" for x in feats))
        parts.append(lc.call_to_action.strip())
        parts += base[lang]["disclaimers"]
        content[lang] = {**base[lang], "title": lc.title.strip(), "description": "\n\n".join(parts),
                         "call_to_action": lc.call_to_action.strip(), "search_terms": lc.search_terms[:8]}
    meta = {"model": result.model, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd}
    return content, uncertainty + [f"AI note: {u}" for u in copy.uncertainty], meta
