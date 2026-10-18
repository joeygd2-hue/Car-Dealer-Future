from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, Forbidden, Unprocessable
from app.core.tenancy import Actor, ClientContext
from app.db import tenant_session
from app.listings import service
from app.listings.dealer_settings import get_settings
from app.listings.facts import build_facts
from app.listings.guardrails import check_content, warning_key
from app.models import ListingRevision, Vehicle
from tests.conftest import run_worker
from tests.helpers import configure_dealer, import_csv, listing_for, vehicle_by_stock


def _setup(world):
    ctx, _, src = import_csv(world["a"].id)
    configure_dealer(ctx)
    return ctx, src


def test_bilingual_generation_uses_only_verified_facts(world):
    ctx, _ = _setup(world)
    camry = vehicle_by_stock(ctx, "B200")
    rev = service.generate_listing(ctx, listing_for(ctx, camry).id)
    en, es = rev.content["en"], rev.content["es"]
    assert en["title"] == "2019 Toyota Camry SE"
    assert es["title"].startswith("2019 Toyota Camry SE")
    assert "45,000 miles" in en["description"] and "45,000 millas" in es["description"]
    assert "$18,995" in en["description"]
    assert "cámara de reversa" in es["features"]
    assert "Price excludes tax, title and license." in en["description"]
    assert "El precio no incluye impuestos, título ni placas." in es["description"]
    assert len(en["faq"]) == 7 and len(es["faq"]) == 7
    # never claims warranty/accident/financing facts it does not have
    text = (en["description"] + " ".join(q["a"] for q in en["faq"])).lower()
    for banned in ("accident-free", "clean title", "one owner", "warranty included", "financing available"):
        assert banned not in text
    assert rev.guardrails["errors"] == []
    assert rev.origin == "template_generator"
    assert listing_for(ctx, camry).status == "awaiting_approval"
    assert rev.facts["vehicle"]["year"] == 2019


def test_worker_generates_drafts_after_import(world):
    ctx, _ = _setup(world)
    run_worker()
    ctx.db.expire_all()
    statuses = {listing_for(ctx, v).status for v in ctx.db.scalars(select(Vehicle)).all()}
    assert statuses == {"awaiting_approval"}


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
def test_guardrails_catch_misleading_content(world, text, code):
    ctx, _ = _setup(world)
    camry = vehicle_by_stock(ctx, "B200")
    lst = listing_for(ctx, camry)
    service.generate_listing(ctx, lst.id)
    rev = service.edit_listing(ctx, lst.id, {"en": {"description": text + "\n\nPrice excludes tax, title and license."},
                                             "es": {"description": text + "\n\nEl precio no incluye impuestos, título ni placas."}})
    assert code in {e["code"] for e in rev.guardrails["errors"]}, rev.guardrails
    assert listing_for(ctx, camry).status == "draft_generated"
    with pytest.raises(Unprocessable):
        service.approve_listing(ctx_admin(ctx), lst.id, rev.id)


def ctx_admin(ctx) -> ClientContext:
    return ClientContext(client_id=ctx.client_id, role="dealer_admin", db=ctx.db, actor=Actor("user", "usr_test"))


def test_supported_claims_are_allowed(world):
    ctx, src = _setup(world)
    csv = "Stock #,Year,Make,Model,Miles,Price,Title,Owners,Condition,Photo URLs\nT1,2020,Honda,Civic,20000,19000,Clean,1,CPO,https://img.example/t1.jpg\n"
    import_csv(world["a"].id, csv, full_snapshot=False, source_name="Extra")
    ctx.db.expire_all()
    v = vehicle_by_stock(ctx, "T1")
    s, _ = get_settings(ctx)
    facts = build_facts(ctx.db, v, s)
    content = {"languages": ["en"], "en": {"title": "2020 Honda Civic", "description": "One owner, clean title, certified pre-owned. "
                                           "Price excludes tax, title and license.", "features": [], "faq": [], "search_terms": []}}
    s.languages = ["en"]
    errors = {e["code"] for e in check_content(facts, content, s)["errors"]}
    assert "unsupported_claim" not in errors


def test_missing_disclaimer_and_required_fields_block(world):
    ctx, src = _setup(world)
    import_csv(world["a"].id, "Stock #,Year,Make,Model\nN1,2015,Kia,Soul\n", full_snapshot=False, source_name="Extra")
    v = vehicle_by_stock(ctx, "N1")
    rev = service.generate_listing(ctx, listing_for(ctx, v).id)
    codes = {e["code"] for e in rev.guardrails["errors"]}
    assert "missing_required_field" in codes  # price, mileage, photos required by default settings
    assert any("price" in u.lower() for u in rev.uncertainty)  # uncertainty flagged, not invented
    rev2 = service.edit_listing(ctx, listing_for(ctx, v).id, {"en": {"description": "Nice Soul."}})
    assert "missing_disclaimer" in {e["code"] for e in rev2.guardrails["errors"]}


def test_approval_requires_acknowledged_warnings_and_current_facts(world):
    ctx, src = _setup(world)
    camry = vehicle_by_stock(ctx, "B200")
    lst = listing_for(ctx, camry)
    service.generate_listing(ctx, lst.id)
    rev = service.edit_listing(ctx, lst.id, {"en": {"description": "Like new inside and out.\n\nPrice excludes tax, title and license."}})
    assert rev.guardrails["errors"] == [] and rev.guardrails["warnings"]
    admin = ctx_admin(ctx)
    with pytest.raises(Unprocessable) as exc:
        service.approve_listing(admin, lst.id, rev.id, [])
    assert "not acknowledged" in str(exc.value.details)
    # price changes after the draft -> approving the stale draft is refused
    ctx.db.commit()
    import_csv(world["a"].id, open_sample().replace("18995", "17995"), source=src)
    ctx.db.expire_all()
    with pytest.raises((Unprocessable, Conflict)):
        service.approve_listing(admin, lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    # regenerate, then approval succeeds
    rev3 = service.generate_listing(ctx, lst.id)
    assert "$17,995" in rev3.content["en"]["description"]
    service.approve_listing(admin, lst.id, rev3.id, [warning_key(w) for w in rev3.guardrails["warnings"]])
    assert listing_for(ctx, camry).status == "approved"


def open_sample():
    from tests.conftest import SAMPLE_CSV
    return SAMPLE_CSV


def test_agency_operator_cannot_approve_unless_dealer_allows(world):
    ctx, _ = _setup(world)
    camry = vehicle_by_stock(ctx, "B200")
    lst = listing_for(ctx, camry)
    rev = service.generate_listing(ctx, lst.id)
    op = ClientContext(client_id=ctx.client_id, role="agency_operator", db=ctx.db, actor=Actor("user", "usr_op"))
    with pytest.raises(Forbidden):
        service.approve_listing(op, lst.id, rev.id)
    from app.listings.dealer_settings import update_settings
    update_settings(ctx, {"review": {"allow_agency_approval": True}})
    service.approve_listing(op, lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    assert lst.status == "approved"


def test_batch_approval_safeguards(world):
    ctx, src = _setup(world)
    ctx.db.commit()
    import_csv(world["a"].id, "Stock #,Year,Make,Model\nN1,2015,Kia,Soul\n", full_snapshot=False, source_name="Extra")
    ids = []
    for v in ctx.db.scalars(select(Vehicle)).all():
        lst = listing_for(ctx, v)
        service.generate_listing(ctx, lst.id)
        ids.append(lst.id)
    admin = ctx_admin(ctx)
    preview = service.batch_preview(admin, ids)
    assert preview["eligible_count"] == 3  # Kia Soul lacks price/mileage/photos
    with pytest.raises(Exception):
        service.batch_approve(admin, ids, expected_count=3, confirm=False)
    with pytest.raises(Conflict):
        service.batch_approve(admin, ids, expected_count=4, confirm=True)
    res = service.batch_approve(admin, ids, expected_count=3, confirm=True, acknowledge_all_warnings=True)
    assert len(res["approved"]) == 3 and len(res["skipped"]) == 1


def test_revisions_are_immutable_and_history_kept(world):
    ctx, _ = _setup(world)
    lst = listing_for(ctx, vehicle_by_stock(ctx, "B200"))
    r1 = service.generate_listing(ctx, lst.id)
    r2 = service.edit_listing(ctx, lst.id, {"en": {"title": "2019 Toyota Camry SE Sedan"}}, note="clarify body style")
    ctx.db.commit()
    revs = ctx.db.scalars(select(ListingRevision).where(ListingRevision.listing_id == lst.id)).all()
    assert [r.revision_number for r in revs] == [1, 2] and r2.origin == "human_edit"
    assert r1.content["en"]["title"] == "2019 Toyota Camry SE"


def test_ai_generator_output_still_passes_through_guardrails(world, monkeypatch):
    """Simulates an AI response that invents a warranty: it must be stored with a blocking error."""
    from app.config import settings
    from app.listings import ai_generator
    from app.core import ai
    monkeypatch.setattr(settings, "listing_ai_enabled", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake_call(system, user, schema, **kw):
        assert "18995" in user  # facts are passed
        lc = ai_generator.LangCopy(title="2019 Toyota Camry SE", description="Comes with a factory warranty.",
                                   call_to_action="Call us.", search_terms=["toyota camry"])
        return ai.AIResult(parsed=ai_generator.ListingCopy(en=lc, es=lc, uncertainty=[]), model="claude-opus-5-5",
                           input_tokens=1000, output_tokens=500)
    monkeypatch.setattr(ai_generator, "structured_call", fake_call)
    ctx, _ = _setup(world)
    rev = service.generate_listing(ctx, listing_for(ctx, vehicle_by_stock(ctx, "B200")).id)
    assert rev.origin == "ai_generator" and rev.generator_model == "claude-opus-5-5"
    assert "unsupported_claim" in {e["code"] for e in rev.guardrails["errors"]}
