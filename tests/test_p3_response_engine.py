"""Phase 3 responder: grounded answers, missing/stale data, unknown questions, output validation, LLM guardrails."""

from datetime import datetime, timedelta

import pytest

from dealerflow.db import utcnow
from dealerflow.models import Client
from dealerflow.models_dealer import Vehicle
from dealerflow.services.conversations.automation_policy import get_settings_row
from dealerflow.services.conversations.responder import analysis as A
from dealerflow.services.conversations.responder.engine import compose, llm_rephrase
from dealerflow.services.conversations.responder.facts import build_fact_sheet
from dealerflow.services.conversations.responder.safety import validate_output
from tests.phase3 import build_world

NOW = datetime(2026, 10, 14, 10, 0)


@pytest.fixture
def world(db):
    return build_world(db)


def plan_for(db, world, text, lang="en", vehicle=None):
    d = db.get(Client, world["a"]["d"].id)
    v = vehicle if vehicle is not None else db.get(Vehicle, world["a"]["civic"].id)
    fs = build_fact_sheet(db, d, get_settings_row(db, d.id), lang, v)
    an = A.analyze(text, lang, NOW)
    return compose(an, fs, text, contact_name=None, has_contact_method=False, already_offered_test_drive=False,
                   is_first_reply=False, now_local=NOW), fs


def test_answers_from_verified_inventory_en(db, world):
    p, _ = plan_for(db, world, "What's the price and mileage on the Civic? Is it available?")
    assert "$18,995" in p.body and "45,210" in p.body and "available" in p.body
    assert not p.escalation_reasons and not p.validation_errors


def test_answers_in_spanish(db, world):
    p, _ = plan_for(db, world, "¿Cuánto cuesta el Civic?", "es")
    assert "El precio publicado es de $18,995." in p.body and p.language == "es"


def test_stale_inventory_needs_confirmation(db, world):
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.verified_at = v.last_seen_at = utcnow() - timedelta(days=5)
    db.flush()
    p, _ = plan_for(db, world, "Is the Civic available and how much?")
    assert "$18,995" not in p.body and "confirm" in p.body
    assert "uncertain_inventory" in p.escalation_reasons


def test_feed_marked_stale_needs_confirmation(db, world):
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.stale = True  # Phase 2: the feed stopped listing this vehicle
    db.flush()
    p, _ = plan_for(db, world, "How much is the Civic?")
    assert "$18,995" not in p.body and "uncertain_inventory" in p.escalation_reasons


def test_missing_price_and_conflict(db, world):
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.price_cents = None
    db.flush()
    p, _ = plan_for(db, world, "How much is the Civic?")
    assert "missing_data" in p.escalation_reasons and "$" not in p.body
    v.price_cents, v.data_conflict_note = 1899500, "website shows $17,995"
    db.flush()
    p, _ = plan_for(db, world, "How much is the Civic?")
    assert "$" not in p.body and "uncertain_inventory" in p.escalation_reasons


def test_phase2_validation_conflict_withholds_price(db, world):
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.validation_issues = [{"code": "implausible_price", "severity": "warning"}]
    db.flush()
    p, _ = plan_for(db, world, "How much is the Civic?")
    assert "$18,995" not in p.body and "uncertain_inventory" in p.escalation_reasons


def test_sold_vehicle_is_not_offered(db, world):
    v = db.get(Vehicle, world["a"]["civic"].id)
    v.inventory_status = "sold"
    db.flush()
    p, _ = plan_for(db, world, "Is the Civic still available?")
    assert "is available" not in p.body.lower()


def test_unknown_question_escalates(db, world):
    p, _ = plan_for(db, world, "Do you guys sponsor the little league team this year?")
    assert "unknown_question" in p.escalation_reasons and p.confidence < 0.85


def test_unapproved_business_info_not_shared(db, world):
    d = db.get(Client, world["a"]["d"].id)
    d.business_info_approved_at = None
    db.flush()
    p, _ = plan_for(db, world, "What are your hours and where are you located?")
    assert "A1 Test Rd" not in p.body and "missing_data" in p.escalation_reasons


def test_approved_hours_and_address_are_shared(db, world):
    p, _ = plan_for(db, world, "What are your hours and where are you located?")
    assert "A1 Test Rd" in p.body and "9" in p.body


def test_never_invents_financing_or_warranty(db, world):
    p, _ = plan_for(db, world, "Can I get approved for financing? Does it come with a warranty? Any accidents?")
    low = p.body.lower()
    assert "approved for" not in low and "includes a warranty" not in low and "no accidents" not in low
    assert {"financing", "warranty", "condition_history"} <= set(p.escalation_reasons)


def test_validator_flags_invented_content():
    allowed = ["$18,995", "2019 Honda Civic EX"]
    assert validate_output("The price is $18,995.", allowed_sources=allowed, language="en") == []
    assert any("number" in v for v in validate_output("It's $15,000 today only.", allowed_sources=allowed, language="en"))
    assert any("claim" in v for v in validate_output("It has no accidents and a clean title.", allowed_sources=allowed, language="en"))
    assert any("promise" in v for v in validate_output("You're all set, see you Saturday!", allowed_sources=allowed, language="en"))
    assert any("language" in v for v in validate_output("Hola, el carro está disponible para usted hoy mismo",
                                                         allowed_sources=allowed, language="en"))


class FakeLLM:
    def __init__(self, text):
        self.text = text

    def complete(self, *, system, prompt, max_tokens=800):
        return self.text, 0.1


def test_llm_rephrase_rejected_when_it_adds_facts(db, world):
    p, fs = plan_for(db, world, "How much is the Civic?")
    bad, _ = llm_rephrase(FakeLLM("Great news! The Civic is $18,995 and we can do $500 off with approved financing."), p, fs)
    assert bad is None
    good, _ = llm_rephrase(FakeLLM("Thanks for asking! The listed price for the Civic is $18,995."), p, fs)
    assert good and "$18,995" in good
    dropped, _ = llm_rephrase(FakeLLM("Thanks for asking about the Civic!"), p, fs)
    assert dropped is None  # dropped the verified price
