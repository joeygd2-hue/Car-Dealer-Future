"""Language detection, opt-out recognition, sensitive-data redaction, time parsing, escalation topics.

Ported from app/tests/test_language_analysis.py to dealerflow.services.responder."""

from __future__ import annotations

from datetime import datetime

import pytest

from dealerflow.services.responder import analysis as A
from dealerflow.services.responder.language import detect_language


@pytest.mark.parametrize("text,lang", [
    ("Is the Civic still available?", "en"), ("¿Todavía está disponible el Civic?", "es"),
    ("Cuánto cuesta la troca", "es"), ("What are your hours on Saturday?", "en"),
])
def test_language_detection(text, lang):
    assert detect_language(text)[0] == lang


@pytest.mark.parametrize("text", [
    "STOP", "stop", "Unsubscribe", "Please stop texting me", "don't contact me again", "ALTO", "Parar",
    "No me escriban más", "dejen de escribirme", "Quiero darme de baja", "BAJA", "cancel",
])
def test_opt_out_recognized(text):
    assert A.is_opt_out(text)


@pytest.mark.parametrize("text", [
    "Can I stop by Saturday?", "I want to cancel my appointment", "Is it still available?", "para cuando lo tienen?",
])
def test_not_opt_out(text):
    a = A.analyze(text, "en", datetime(2026, 10, 14, 10))
    assert not a.opt_out


def test_human_request_and_frustration():
    a = A.analyze("This is ridiculous, I want to talk to a real person", "en")
    assert a.human_request and a.frustration


def test_sensitive_detection_and_redaction():
    text = "SSN 123-45-6789 and card 4111 1111 1111 1111"
    assert set(A.detect_sensitive(text)) >= {"ssn", "payment_card"}
    red = A.redact(text)
    assert "123-45-6789" not in red and "4111" not in red
    assert A.detect_sensitive(red)  # redaction markers still flag the message


def test_time_parsing_en_es():
    now = datetime(2026, 10, 14, 10, 0)  # Wednesday
    dt, _ = A.parse_requested_time("Saturday at 11am", now)
    assert dt == datetime(2026, 10, 17, 11, 0)
    dt, _ = A.parse_requested_time("mañana a las 3", now)
    assert dt == datetime(2026, 10, 15, 15, 0)
    dt, _ = A.parse_requested_time("en la mañana", now)
    assert dt is None


def test_escalation_topics_detected():
    a = A.analyze("Do you finance? Is there a warranty? Can I trade in my car? Any accidents?", "en")
    assert {A.FINANCING, A.WARRANTY, A.TRADE_IN, A.CONDITION} <= set(a.intents)
