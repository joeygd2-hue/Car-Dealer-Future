"""Detection of payment-card numbers and redaction of secrets/PII from logs."""
from __future__ import annotations

import logging
import re

_CARD_CANDIDATE = re.compile(r"(?:\d[ -]?){13,19}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?1?[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}")
_SECRET_KV = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie)(\"?\s*[:=]\s*\"?)([^\s\",;]+)"
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_KEYS = re.compile(r"\b(sk|rk|pk|whsec|sk-ant)[-_][A-Za-z0-9_-]{8,}\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def contains_card_number(text: str | None) -> bool:
    """True if the text appears to contain a payment card number (PAN)."""
    if not text:
        return False
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


def assert_no_card_data(**fields: str | None) -> None:
    for name, value in fields.items():
        if contains_card_number(value):
            raise ValueError(
                f"Field '{name}' appears to contain a payment card number. "
                "Card details must never be stored; record only the processor reference."
            )


def redact(text: str) -> str:
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KNOWN_KEYS.sub("[REDACTED_KEY]", text)
    text = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    text = _CARD_CANDIDATE.sub(
        lambda m: "[card]" if _luhn_ok(re.sub(r"\D", "", m.group())) else m.group(), text
    )
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that strips secrets and personal data from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    return "•" * max(0, len(value) - keep) + value[-keep:]
