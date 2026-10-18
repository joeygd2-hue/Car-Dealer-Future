"""Reports use stored data and keep drafts, requests and simulations separate from real results."""

from sqlalchemy import select

from dealerflow.models import Template
from dealerflow.services import outreach, reports
from dealerflow.services.discovery import run_source
from dealerflow.services.prospects import LeadRecord, intake


def test_draft_is_not_sent_and_simulated_is_separate(db, admin):
    run_source(db, "fixture")
    real = intake(db, LeadRecord(name="Real Dealer", source="manual", email="owner@realdealer.example",
                                 dealership_type="independent_used", city="Sugar Land")).prospect
    tpl = db.scalar(select(Template).where(Template.name == "intro", Template.language == "en"))
    outreach.create_draft(db, real, tpl, actor=admin)
    d = reports.daily(db)
    assert d["awaiting_approval"]["simulated"] == 1  # mock provider => simulated message
    assert d["sent"] == {"real": 0, "simulated": 0}
    assert d["new_leads"] == {"real": 1, "simulated": 7}
    w = reports.weekly(db)
    funnel = {f["stage"]: f["count"] for f in w["funnel"]}
    assert funnel["Discovered"] == 1 and funnel["Contacted (actually sent)"] == 0
    assert w["cac"] is None and "Not calculable" in w["cac_note"]
    assert any("waiting for approval" in r for r in w["recommendations"])


def test_simulated_send_does_not_mark_real_prospect_contacted(db, admin):
    from dealerflow.jobs import run_pending
    from dealerflow.models import Prospect

    real = intake(db, LeadRecord(name="Real Dealer 2", source="manual", email="owner@real2.example",
                                 dealership_type="independent_used", city="Sugar Land")).prospect
    tpl = db.scalar(select(Template).where(Template.name == "intro", Template.language == "en"))
    m = outreach.create_draft(db, real, tpl, actor=admin)
    outreach.approve(db, m, actor=admin)
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Prospect, real.id).stage != "contacted"
    assert reports.weekly(db)["sent"] == 0
