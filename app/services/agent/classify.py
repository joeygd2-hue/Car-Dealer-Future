"""Reply classification (English and Spanish).

Rule-based first; precedence is safety-first: bounce > opt_out > sensitive >
out_of_office > not_interested > meeting_request > positive > question.
An optional AI pass may refine `unknown` results only; AI output never
overrides a rule match and always routes to a human unless it matches a
routine category with high confidence.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

RULES: list[tuple[str, re.Pattern]] = [
    ("bounce", re.compile(r"(mailer-daemon|postmaster@|delivery status notification|undeliverable|delivery has failed|address not found|mailbox (is )?(full|unavailable)|550 5\.\d\.\d)", re.I)),
    ("opt_out", re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|do not (contact|email)|don'?t (contact|email)|opt[ -]?out|no (me )?(contacten|escriban|envíen)|darme de baja|dar de baja|quítenme|eliminen mi correo)\b|^\s*stop\s*$", re.I | re.M)),
    ("sensitive", re.compile(r"\b(lawyer|attorney|legal action|sue|lawsuit|cease and desist|complaint|harass|spam(ming)?|report you|ftc|attorney general|abogado|demanda|queja|acoso)\b", re.I)),
    ("out_of_office", re.compile(r"\b(out of (the )?office|on vacation|away until|auto(matic)? reply|autoreply|fuera de la oficina|de vacaciones|respuesta automática)\b", re.I)),
    ("not_interested", re.compile(r"\b(not interested|no thanks|no thank you|we('re| are) (all )?set|already have (a|an|someone)|not at this time|no estamos interesados|no me interesa|no gracias|ya tenemos)\b", re.I)),
    ("meeting_request", re.compile(r"\b(schedule|book|set up|hop on|jump on) (a )?(call|meeting|time|demo)|\b(call me|let'?s (talk|meet|chat)|what times?|available (on|at|this|next)|option [1-3]|#[1-3]\b|agendar|reunión|llamada|llámame|cuándo puede)", re.I)),
    ("positive", re.compile(r"\b(interested|sounds good|tell me more|learn more|send (me )?(more )?info|yes,? please|me interesa|cuéntame más|más información|suena bien)\b", re.I)),
    ("question", re.compile(r"\?|\b(how much|what does it cost|price|pricing|how does|what is|who are you|cuánto|precio|cómo funciona|quiénes son)\b", re.I)),
]

# Topics that are commitments we must not make automatically.
COMMITMENT_TOPICS = re.compile(r"\b(guarantee|contract|discount|refund|cancel|exclusive|results|roi|garant[ií]a|contrato|descuento|reembolso)\b", re.I)

ROUTINE = {"question", "positive", "meeting_request", "not_interested", "out_of_office"}


@dataclass
class Classification:
    label: str
    method: str
    confidence: float
    needs_human: bool
    language: str


def detect_language(text: str) -> str:
    es = len(re.findall(r"\b(el|la|los|las|que|de|para|por|gracias|usted|nosotros|está|qué|cómo|sí)\b", text, re.I))
    en = len(re.findall(r"\b(the|and|for|you|we|our|thanks|please|is|are|what|how|yes)\b", text, re.I))
    return "es" if es > en else "en"


def strip_quoted(body: str) -> str:
    """Remove quoted previous messages so we classify only the new text."""
    out = []
    for line in body.splitlines():
        if line.strip().startswith(">") or re.match(r"^\s*On .+ wrote:\s*$", line) or re.match(r"^\s*El .+ escribió:\s*$", line):
            break
        out.append(line)
    return "\n".join(out).strip() or body.strip()


def classify(from_address: str, subject: str | None, body: str, headers: dict | None = None) -> Classification:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    text = f"{subject or ''}\n{strip_quoted(body)}"
    lang = detect_language(text)
    if headers.get("auto-submitted", "no").lower() not in ("", "no") and not RULES[0][1].search(from_address + text):
        return Classification("out_of_office", "rule", 0.9, False, lang)
    for label, pattern in RULES:
        target = f"{from_address}\n{text}" if label == "bounce" else text
        if pattern.search(target):
            needs_human = label in ("sensitive",) or (label in ("question", "positive", "meeting_request") and bool(COMMITMENT_TOPICS.search(text)))
            return Classification(label, "rule", 0.8, needs_human, lang)
    return Classification("unknown", "rule", 0.0, True, lang)


def ai_refine(db: Session, text: str, base: Classification) -> Classification:
    """Optionally ask the AI to classify an `unknown` reply. Result still goes to a human if not routine."""
    from app.integrations.llm import AIUnavailable, complete

    if base.label != "unknown":
        return base
    try:
        res = complete(
            db,
            system="Classify a reply to a B2B sales email. Respond with JSON only: {\"label\": one of "
            "[positive, meeting_request, question, not_interested, opt_out, out_of_office, sensitive, unknown], \"confidence\": 0-1}.",
            prompt=text[:4000], feature="agent.classify", max_tokens=200, effort="low",
        )
    except AIUnavailable:
        return base
    try:
        data = json.loads(res.text.strip().strip("`").removeprefix("json"))
        label, conf = str(data["label"]), float(data["confidence"])
    except (ValueError, KeyError, TypeError):
        return base
    if label == "opt_out":
        return Classification("opt_out", "ai", conf, False, base.language)  # honour any possible opt-out
    needs_human = not (label in ROUTINE and conf >= 0.85)
    return Classification(label, "ai", conf, needs_human, base.language)
