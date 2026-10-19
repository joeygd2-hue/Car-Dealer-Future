"""Output validation: a reply may only contain facts present in the fact sheet / approved content."""

from __future__ import annotations

import re

from .language import detect_language, strip_accents

# Affirmative claims the assistant must never make on its own.
BANNED_CLAIMS = re.compile("|".join([
    r"pre-?approved", r"guaranteed (approval|financing)", r"you('re| are) approved", r"approved for (financing|a loan)",
    r"no accidents?", r"accident[- ]free", r"clean (title|carfax|history)", r"(comes with|includes?|covered by) (a |an )?(\w+ )?warranty",
    r"warranty (included|covered)", r"\d+(\.\d+)?\s?% ?(apr|interest)", r"\$\s?[\d,]+ off\b", r"\bdiscount(ed)? (of|price)\b",
    r"special (offer|price|deal|financing)", r"we can (do|go) (lower|\$)", r"best price (in|guaranteed)",
    r"\baprobad[oa]\b", r"garantizad[oa]", r"sin accidentes", r"titulo limpio", r"incluye garantia", r"descuento de",
    r"oferta especial", r"precio especial", r"no (tiene|ha tenido) (accidentes|choques)",
]), re.IGNORECASE)

# Phrases that would promise an appointment before the booking system confirms it.
PROMISE_RX = re.compile("|".join([
    r"\byou'?re (all )?(set|booked|confirmed)\b", r"\b(appointment|test drive) is (confirmed|booked|scheduled)\b",
    r"\bsee you (on|at|then|tomorrow|saturday|sunday|monday|tuesday|wednesday|thursday|friday)\b",
    r"\b(cita|prueba de manejo) (esta|quedo) (confirmada|agendada)\b", r"\blo esperamos el\b", r"\bnos vemos el\b",
]), re.IGNORECASE)

NUMBER_RX = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER_RX.findall(text)}


def validate_output(text: str, *, allowed_sources: list[str], language: str,
                    appointment_confirmed: bool = False, required_strings: list[str] | None = None) -> list[str]:
    """Return a list of violations (empty = safe)."""
    violations: list[str] = []
    allowed_text = "\n".join(allowed_sources)
    allowed_numbers = _numbers(allowed_text)
    for n in _numbers(text):
        if n not in allowed_numbers:
            violations.append(f"unsupported number: {n}")
    norm = strip_accents(text.lower())
    allowed_norm = strip_accents(allowed_text.lower())
    for m in BANNED_CLAIMS.finditer(norm):
        if m.group(0) not in allowed_norm:
            violations.append(f"unsupported claim: {m.group(0)}")
    if not appointment_confirmed:
        for m in PROMISE_RX.finditer(norm):
            violations.append(f"unconfirmed appointment promise: {m.group(0)}")
    if len(text.split()) >= 6:
        detected, conf = detect_language(text, prior=language)
        if detected != language and conf >= 0.6:
            violations.append(f"language mismatch: expected {language}, got {detected}")
    for s in required_strings or []:
        if s and s not in text:
            violations.append(f"dropped required fact: {s}")
    return violations
