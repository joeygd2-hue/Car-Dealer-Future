"""Phase 2 listing generation, guardrails, approval and batch safeguards."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from dealerflow.models_dealer import ListingRevision, Vehicle
from dealerflow.services.dealer_errors import Conflict, Forbidden, Unprocessable
from dealerflow.services.listings import service
from dealerflow.services.listings.dealer_settings import get_settings, update_settings
from dealerflow.services.listings.facts import build_facts
from dealerflow.services.listings.guardrails import check_content, warning_key
from tests.dealer_helpers import (SAMPLE_CSV, configure_dealer, ctx_as, drain, import_csv, listing_for, make_client,
                                  make_dealer_user, make_staff, vehicle_by_stock)


@pytest.fixture
def setup():
    cid = make_client()
    ctx, _, _ = import_csv(cid)
    configure_dealer(ctx)
    owner = make_dealer_user(cid, "owner@alpha.example")
    yield cid, ctx, owner
    ctx.close()


def as_owner(cid, owner):
    return ctx_as(cid, owner)


def test_bilingual_generation_uses_only_verified_facts(setup):
    cid, ctx, _ = setup
    camry = vehicle_by_stock(ctx, "B200")
    rev = service.generate_listing(ctx, listing_for(ctx, camry).id)
    en, es = rev.content["en"], rev.content["es"]
    assert en["title"] == "2019 Toyota Camry SE" and es["title"].startswith("2019 Toyota Camry SE")
    assert "45,000 miles" in en["description"] and "45,000 millas" in es["description"] and "$18,995" in en["description"]
    assert "cámara de reversa" in es["features"]
    assert "Price excludes tax, title and license." in en["description"]
    assert "El precio no incluye impuestos, título ni placas." in es["description"]
    assert len(en["faq"]) == 7 and len(es["faq"]) == 7
    text = (en["description"] + " ".join(q["a"] for q in en["faq"])).lower()
    for banned in ("accident-free", "clean title", "one owner", "warranty included", "financing available"):
        assert banned not in text
    assert rev.guardrails["errors"] == [] and rev.origin == "template_generator"
    assert listing_for(ctx, camry).status == "awaiting_approval"


def test_worker_generates_drafts_after_import(setup):
    cid, ctx, _ = setup
    drain()
    ctx.db.expire_all()
    assert {listing_for(ctx, v).status for v in ctx.db.scalars(select(Vehicle))} == {"awaiting_approval"}


@pytest.mark.parametrize("text,code", [
    ("This Camry won't last, act now!", "misleading_urgency"),
    ("¡No te lo pierdas!", "misleading_urgency"),
    ("Accident-free and comes with warranty included.", "unsupported_claim"),
    ("One owner, clean title.", "unsupported_claim"),
    ("Guaranteed approval, no credit check.", "unsupported_claim"),
    ("Only 30,000 miles on this beauty.", "spec_mismatch"),
    ("A 2021 model for $15,000.", "spec_mismatch"),
    ("Capable AWD for any weather.", "spec_mismatch"),
    ("camry camry camry camry camry camry camry camry camry camry used camry", "keyword_stuffing"),
])
def test_guardrails_block_misleading_content(setup, text, code):
    cid, ctx, owner = setup
    lst = listing_for(ctx, vehicle_by_stock(ctx, "B200"))
    service.generate_listing(ctx, lst.id)
    rev = service.edit_listing(ctx, lst.id, {"en": {"description": text + "\n\nPrice excludes tax, title and license."},
                                             "es": {"description": text + "\n\nEl precio no incluye impuestos, título ni placas."}})
    assert code in {e["code"] for e in rev.guardrails["errors"]}, rev.guardrails
    assert lst.status == "draft_generated"
    ctx.db.commit()
    oc = as_owner(cid, owner)
    with pytest.raises(Unprocessable):
        service.approve_listing(oc, lst.id, rev.id)
    oc.close()


def test_supported_claims_are_allowed(setup):
    cid, ctx, _ = setup
    ctx.db.commit()
    import_csv(cid, "Stock #,Year,Make,Model,Miles,Price,Title,Owners,Condition,Photo URLs\n"
                    "T1,2020,Honda,Civic,20000,19000,Clean,1,CPO,https://img.example/t1.jpg\n", source_name="Extra", full_snapshot=False)[0].close()
    ctx.db.expire_all()
    s, _ = get_settings(ctx)
    s.languages = ["en"]
    facts = build_facts(ctx.db, vehicle_by_stock(ctx, "T1"), s)
    content = {"languages": ["en"], "en": {"title": "2020 Honda Civic", "description": "One owner, clean title, certified pre-owned. "
                                           "Price excludes tax, title and license.", "features": [], "faq": [], "search_terms": []}}
    assert "unsupported_claim" not in {e["code"] for e in check_content(facts, content, s)["errors"]}


def test_missing_data_is_flagged_not_invented(setup):
    cid, ctx, _ = setup
    ctx.db.commit()
    import_csv(cid, "Stock #,Year,Make,Model\nN1,2015,Kia,Soul\n", source_name="Extra", full_snapshot=False)[0].close()
    ctx.db.expire_all()
    v = vehicle_by_stock(ctx, "N1")
    rev = service.generate_listing(ctx, listing_for(ctx, v).id)
    assert "missing_required_field" in {e["code"] for e in rev.guardrails["errors"]}
    assert any("price" in u.lower() for u in rev.uncertainty)
    rev2 = service.edit_listing(ctx, listing_for(ctx, v).id, {"en": {"description": "Nice Soul."}})
    assert "missing_disclaimer" in {e["code"] for e in rev2.guardrails["errors"]}


def test_approval_requires_acknowledged_warnings_and_current_facts(setup):
    cid, ctx, owner = setup
    lst = listing_for(ctx, vehicle_by_stock(ctx, "B200"))
    service.generate_listing(ctx, lst.id)
    rev = service.edit_listing(ctx, lst.id, {"en": {"description": "Like new inside and out.\n\nPrice excludes tax, title and license."}})
    assert rev.guardrails["errors"] == [] and rev.guardrails["warnings"]
    ctx.db.commit()
    oc = as_owner(cid, owner)
    with pytest.raises(Unprocessable) as exc:
        service.approve_listing(oc, lst.id, rev.id, [])
    assert "not acknowledged" in str(exc.value.details)
    oc.close()
    import_csv(cid, SAMPLE_CSV.replace("18995", "17995"))[0].close()  # price changes after the draft
    oc = as_owner(cid, owner)
    with pytest.raises((Unprocessable, Conflict)):
        service.approve_listing(oc, lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    rev3 = service.generate_listing(oc, lst.id)
    assert "$17,995" in rev3.content["en"]["description"]
    service.approve_listing(oc, lst.id, rev3.id, [warning_key(w) for w in rev3.guardrails["warnings"]])
    assert service.get_listing(oc, lst.id).status == "approved"
    oc.close()


def test_agency_staff_approve_only_when_dealer_allows(setup):
    cid, ctx, _ = setup
    update_settings(ctx, {"review": {"allow_agency_approval": False}})
    lst = listing_for(ctx, vehicle_by_stock(ctx, "B200"))
    rev = service.generate_listing(ctx, lst.id)
    ctx.db.commit()
    staff = make_staff("rep@agency.example", role="admin")
    sc = ctx_as(cid, staff)
    with pytest.raises(Forbidden):
        service.approve_listing(sc, lst.id, rev.id)
    update_settings(sc, {"review": {"allow_agency_approval": True}})
    service.approve_listing(sc, lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    assert service.get_listing(sc, lst.id).status == "approved"
    sc.close()
    viewer = make_dealer_user(cid, "viewer@alpha.example", role="dealer_viewer")
    vc = ctx_as(cid, viewer)
    with pytest.raises(Forbidden):
        service.reject_listing(vc, lst.id, "no")
    vc.close()


def test_batch_approval_safeguards(setup):
    cid, ctx, owner = setup
    ctx.db.commit()
    import_csv(cid, "Stock #,Year,Make,Model\nN1,2015,Kia,Soul\n", source_name="Extra", full_snapshot=False)[0].close()
    oc = as_owner(cid, owner)
    ids = []
    for v in oc.db.scalars(select(Vehicle)).all():
        lst = listing_for(oc, v)
        service.generate_listing(oc, lst.id)
        ids.append(lst.id)
    assert service.batch_preview(oc, ids)["eligible_count"] == 3  # the Kia lacks price/mileage/photos
    with pytest.raises(Exception):
        service.batch_approve(oc, ids, expected_count=3, confirm=False)
    with pytest.raises(Conflict):
        service.batch_approve(oc, ids, expected_count=4, confirm=True)
    res = service.batch_approve(oc, ids, expected_count=3, confirm=True, acknowledge_all_warnings=True)
    assert len(res["approved"]) == 3 and len(res["skipped"]) == 1
    oc.close()


def test_revisions_are_kept_with_history(setup):
    cid, ctx, _ = setup
    lst = listing_for(ctx, vehicle_by_stock(ctx, "B200"))
    r1 = service.generate_listing(ctx, lst.id)
    r2 = service.edit_listing(ctx, lst.id, {"en": {"title": "2019 Toyota Camry SE Sedan"}}, note="clarify body style")
    ctx.db.commit()
    revs = ctx.db.scalars(select(ListingRevision).where(ListingRevision.listing_id == lst.id).order_by(ListingRevision.revision_number)).all()
    assert [r.revision_number for r in revs] == [1, 2] and r2.origin == "human_edit" and r1.content["en"]["title"] == "2019 Toyota Camry SE"


def test_ai_output_still_passes_through_guardrails(setup, monkeypatch):
    """An AI response that invents a warranty must be stored with a blocking error."""
    from dealerflow.config import get_settings as app_settings
    from dealerflow.integrations import llm
    from dealerflow.services.listings import ai_generator
    monkeypatch.setenv("DF_LISTING_AI_ENABLED", "true")
    monkeypatch.setenv("DF_ANTHROPIC_API_KEY", "test-key")
    app_settings.cache_clear()

    def fake_call(system, user, schema, **kw):
        assert "18995" in user  # verified facts are passed
        lc = ai_generator.LangCopy(title="2019 Toyota Camry SE", description="Comes with a factory warranty.",
                                   call_to_action="Call us.", search_terms=["toyota camry"])
        return llm.LLMResult(parsed=ai_generator.ListingCopy(en=lc, es=lc, uncertainty=[]), model="claude-opus-5-5",
                             input_tokens=1000, output_tokens=500)
    monkeypatch.setattr(ai_generator, "structured_call", fake_call)
    cid, ctx, _ = setup
    rev = service.generate_listing(ctx, listing_for(ctx, vehicle_by_stock(ctx, "B200")).id)
    assert rev.origin == "ai_generator" and rev.generator_model == "claude-opus-5-5"
    assert "unsupported_claim" in {e["code"] for e in rev.guardrails["errors"]}
