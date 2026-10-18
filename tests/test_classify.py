import pytest

from dealerflow.services.classify import classify

CASES = [
    ("Please remove me from your list", "opt_out"),
    ("STOP", "opt_out"),
    ("Not interested, and don't email me again", "opt_out"),
    ("Por favor no me contacte más", "opt_out"),
    ("Quiero darme de baja", "opt_out"),
    ("I am out of the office until Monday", "auto_reply"),
    ("Respuesta automática: fuera de la oficina", "auto_reply"),
    ("Sounds interesting, can we set up a call next week?", "meeting_request"),
    ("¿Podemos agendar una llamada el martes?", "meeting_request"),
    ("Send me more info please", "interested"),
    ("Me interesa, mándeme más información", "interested"),
    ("How much does it cost?", "question"),
    ("¿Cuánto cuesta?", "question"),
    ("No thanks, we already have someone for that", "not_interested"),
    ("No gracias, no nos interesa", "not_interested"),
    ("Talk to my manager about this", "referral"),
    ("ok", "unclear"),
]


@pytest.mark.parametrize("body,label", CASES)
def test_classify(body, label):
    assert classify("Re: hello", body).label == label


def test_quoted_text_is_ignored():
    body = "Thanks!\n\nOn Mon, Oct 19, 2026 at 9:00 AM Sender wrote:\n> If you'd rather not hear from us, unsubscribe here"
    assert classify("Re: x", body).label != "opt_out"


def test_mixed_signals_are_unclear_and_low_confidence():
    c = classify("Re: x", "Not interested right now but maybe send me more info")
    assert c.label in ("unclear", "not_interested")
    assert c.confidence < 0.9


def test_headers_mark_auto_reply_and_bounce():
    assert classify("x", "body", {"Auto-Submitted": "auto-replied"}).label == "auto_reply"
    assert classify("Undeliverable: hi", "Delivery has failed").label == "bounce"
