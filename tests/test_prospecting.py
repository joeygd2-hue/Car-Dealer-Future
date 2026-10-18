"""Intake, validation, dedup across sources, CSV/XLSX import, merging and scoring."""

import io

from sqlalchemy import func, select

from dealerflow.models import Evidence, Prospect
from dealerflow.services import importer
from dealerflow.services.normalize import missing_fields, normalize_phone, validate_prospect_fields
from dealerflow.services.prospects import LeadRecord, intake, merge_prospects
from dealerflow.services.scoring import score_prospect
from dealerflow.services.settings_store import put_setting

CSV = b"""Dealership Name,Website,Address,City,State,Zip,Phone,Email,Type,Notes
Brazos Bend Motors,brazosbendmotors.example,100 Example Blvd,Sugar Land,TX,77478,281.555.0101,,independent_used,dup of fixture by domain
Totally New Autos (SIMULATED),,,Richmond,TX,77469,(281) 555-0177,bad-email,independent_used,
,,,,,,,,,
No Name Row,,,,ZZ,1234,123,,,
"""


def test_validation_drops_bad_values_and_reports_them():
    out, errs = validate_prospect_fields({"name": "X", "email": "nope", "phone": "12", "state": "ZZ", "postal_code": "1"})
    assert out["email"] is None and out["phone_e164"] is None and out["state"] is None
    assert len(errs) == 4
    assert normalize_phone("+1 (281) 555-0101") == "+12815550101"


def test_csv_dry_run_then_commit_with_dedup(db):
    from dealerflow.services.discovery import run_source

    run_source(db, "fixture")
    before = db.scalar(select(func.count()).select_from(Prospect))
    rows = importer.read_rows("list.csv", CSV)
    rep = importer.import_rows(db, rows, source_label="test list", dry_run=True)
    assert (rep.created, rep.merged, rep.skipped) == (2, 1, 0)
    assert db.scalar(select(func.count()).select_from(Prospect)) == before  # dry run saved nothing
    rep = importer.import_rows(db, rows, source_label="test list")
    assert rep.rows[0]["status"].startswith("merged (domain")
    assert any("email is not valid" in e for e in rep.rows[1]["errors"])
    assert any("state is not" in e for e in rep.rows[2]["errors"])
    p = db.get(Prospect, rep.rows[1]["prospect_id"])
    assert p.lead_source == "csv:test list" and p.email is None
    ev = db.scalar(select(Evidence).where(Evidence.prospect_id == p.id, Evidence.field == "phone_e164"))
    assert ev.source_type == "csv:test list" and ev.collected_at


def test_xlsx_import(db):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Name", "City", "Phone"])
    ws.append(["Excel Cars (SIMULATED)", "Katy", "281-555-0144"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = importer.read_rows("x.xlsx", buf.getvalue())
    rep = importer.import_rows(db, rows, source_label="xlsx")
    assert rep.created == 1


def test_conflicting_values_are_kept_as_evidence_not_overwritten(db):
    a = intake(db, LeadRecord(name="Alpha Autos", source="osm", external_id="n/1", phone="281-555-0111",
                              website="https://alpha.example", city="Stafford")).prospect
    res = intake(db, LeadRecord(name="Alpha Autos", source="google_places", external_id="g1",
                                website="https://alpha.example", phone="281-555-0999", city="Stafford"))
    assert res.prospect.id == a.id and res.matched_by == "domain"
    assert a.phone_e164 == "+12815550111" and res.conflicts
    assert db.scalar(select(Evidence).where(Evidence.value == "+12815550999")).note.startswith("Conflicts")


def test_name_similarity_dedup_needs_same_place(db):
    a = intake(db, LeadRecord(name="Sugar Land Auto Sales", source="manual", city="Sugar Land", postal_code="77478")).prospect
    same = intake(db, LeadRecord(name="Sugar Land Auto Sales LLC", source="manual", city="Sugar Land", postal_code="77478"))
    other_city = intake(db, LeadRecord(name="Sugar Land Auto Sales", source="manual", city="Conroe", postal_code="77301"))
    assert same.prospect.id == a.id and other_city.prospect.id != a.id


def test_manual_merge(db, admin):
    a = intake(db, LeadRecord(name="Beta Cars", source="manual", city="Katy")).prospect
    b = intake(db, LeadRecord(name="B Car Co", source="manual", city="Houston", email="info@betacars.example")).prospect
    merge_prospects(db, a, b, actor_id=admin.id)
    assert b.merged_into_id == a.id and a.email == "info@betacars.example"


def test_score_is_explainable_and_configurable(db, admin):
    p = intake(db, LeadRecord(name="Gamma Motors", source="manual", city="Sugar Land", lat=29.61, lng=-95.63,
                              dealership_type="independent_used", email="x@gamma.example")).prospect
    b = score_prospect(db, p)
    geo = next(f for f in b["factors"] if f["key"] == "geographic_fit")
    assert "miles from the target" in geo["reason"] and geo["value"] == 1.0
    assert "bilingual need unknown" in b["unverified"]
    assert not any(k in str(b).lower() for k in ("revenue", "sales volume", "demographic"))
    put_setting(db, "scoring_weights", {"target_market_fit": 0, "geographic_fit": 100, "inventory_activity": 0,
                                        "web_presence": 0, "contact_available": 0, "bilingual_opportunity": 0,
                                        "engagement": 0}, actor_id=admin.id)
    assert score_prospect(db, p)["total"] == 100.0
    p.do_not_contact = True
    b = score_prospect(db, p)
    assert b["total"] == 0 and not b["eligible"]


def test_missing_fields_indicator(db):
    p = intake(db, LeadRecord(name="Delta", source="manual")).prospect
    assert {"website", "phone_e164", "verified_contact", "dealership_type"} <= set(missing_fields(p))


def test_license_list_columns_become_evidence(db):
    csv_bytes = (b"License Number,License Holder,Assumed Name/DBA,Physical Address,Physical City,Physical Zip,County,License Type\n"
                 b"P123456,ACME HOLDINGS LLC,Acme Used Cars (SIMULATED),1 Test Rd,Sugar Land,77478,Fort Bend,Independent Motor Vehicle GDN\n")
    rep = importer.import_rows(db, importer.read_rows("dealers.csv", csv_bytes), source_label="TxDMV list (test)")
    p = db.get(Prospect, rep.rows[0]["prospect_id"])
    assert p.name == "ACME HOLDINGS LLC" and p.city == "Sugar Land" and p.postal_code == "77478"
    ev = {e.field: e.value for e in p.evidence}
    assert ev["license:number"] == "P123456" and ev["dba"] == "Acme Used Cars (SIMULATED)" and ev["county"] == "Fort Bend"


def test_similar_names_with_different_websites_are_not_merged(db):
    a = intake(db, LeadRecord(name="Gulf Auto Sales 1", source="manual", city="Katy", website="https://gulf1.example")).prospect
    b = intake(db, LeadRecord(name="Gulf Auto Sales 2", source="manual", city="Katy", website="https://gulf2.example")).prospect
    assert a.id != b.id
    c = intake(db, LeadRecord(name="Gulf Auto Sales 2", source="manual", city="Katy", website="https://gulf2.example/x")).prospect
    assert c.id == b.id  # same site: same business
