"""TxDMV dealer-list profile, using SYNTHETIC rows with the real file's column layout
(verified against the list published 2026-10-18; the real file is not stored in the repo)."""

import pytest

from dealerflow.models import Evidence, Prospect
from dealerflow.services import importer
from tests.conftest import login, make_user  # module level: a lazy import re-runs conftest setup

COLS = ["County", "LicenseNumber", "LicenseStatus", "LicenseExpDate", "BusinessName", "DBAName", "AddressType",
        "PhysicalAddress", "PhysAddressTwo", "City", "State", "Zip", "MailingAddress", "MailAddressTwo", "MailingCity",
        "MailingState", "MailingZip", "Phone", "BusinessEmail", "LicenseType", "ActiveDate", "DealerType", "BondCompany"]


def row(**kw):
    base = dict.fromkeys(COLS, "")
    base.update(County="Fort Bend", LicenseStatus="Active", LicenseExpDate="10/31/2027", AddressType="Physical",
                State="Texas", LicenseType="Motor Vehicle", ActiveDate="11/01/2021")
    base.update(kw)
    return base


ROWS = [
    row(LicenseNumber="P900001", BusinessName="SIM HOLDINGS LLC", DBAName="Sim Motors (SIMULATED)", PhysicalAddress="1 Test Rd",
        City="SUGAR LAND", Zip="77478-1234", Phone="(281) 555-0141", BusinessEmail="owner@gmail.com"),
    row(LicenseNumber="P900002", BusinessName="Sim Autos Inc (SIMULATED)", PhysicalAddress="2 Test Rd", City="Richmond",
        Zip="77469", Phone="(281) 555-0142", BusinessEmail="sales@simautos-test.com"),
    row(LicenseNumber="P900003", BusinessName="Sim Franchise (SIMULATED)", DBAName="Sim Toyota", City="Sugar Land",
        DealerType="Franchise", Phone="(281) 555-0143"),
    row(LicenseNumber="P900004", BusinessName="Trailer Co (SIMULATED)", City="Rosenberg", LicenseType="Utility Trailer/Semi-Trailer"),
    row(LicenseNumber="P900005", BusinessName="Expired Lot (SIMULATED)", City="Stafford", LicenseStatus="Expired"),
    row(LicenseNumber="P900006", BusinessName="Far Away Cars (SIMULATED)", County="Webb", City="Laredo"),
    # Different license, same phone as P900001, different address: must stay separate
    row(LicenseNumber="P900007", BusinessName="Other Lot (SIMULATED)", PhysicalAddress="99 Other St", City="Stafford",
        Zip="77477", Phone="(281) 555-0141"),
    row(LicenseNumber="P900001", BusinessName="SIM HOLDINGS LLC", AddressType="Additional", City="Sugar Land"),
]


def test_txdmv_profile_filters_maps_and_keeps_provenance(db):
    with pytest.raises(ValueError, match="TxDMV option"):
        importer.import_rows(db, ROWS, source_label="x", dry_run=True)
    with pytest.raises(ValueError, match="county or city"):
        importer.import_txdmv(db, ROWS, counties=[], cities=[])
    rep = importer.import_txdmv(db, ROWS, counties=["Fort Bend"], cities=[], data_date="2026-10-18", dry_run=True)
    assert rep.created == 3 and db.query(Prospect).count() == 0
    assert rep.excluded == {"franchise dealer": 1, "license type: Utility Trailer/Semi-Trailer": 1,
                            "license status: Expired": 1, "outside selected area": 1, "address type: Additional": 1}
    rep = importer.import_txdmv(db, ROWS, counties=["Fort Bend"], cities=[], data_date="2026-10-18")
    a = db.query(Prospect).filter(Prospect.name == "Sim Motors (SIMULATED)").one()  # DBA preferred
    assert (a.city, a.state, a.postal_code, a.phone_e164) == ("Sugar Land", "TX", "77478", "+12815550141")
    assert a.email is None  # free-mail license email is not an outreach address
    ev = {e.field: e for e in a.evidence}
    assert ev["license_email"].value == "owner@gmail.com" and not ev["license_email"].verified
    assert ev["legal_name"].value == "SIM HOLDINGS LLC" and ev["license:number"].value == "P900001"
    assert not ev["dealership_type"].verified and "Inferred" in ev["dealership_type"].note
    assert all(e.source_url and "texasdmv" in e.source_url for e in a.evidence)
    b = db.query(Prospect).filter(Prospect.name == "Sim Autos Inc (SIMULATED)").one()
    assert b.email == "sales@simautos-test.com"
    assert db.query(Prospect).count() == 3  # P900007 kept separate despite the shared phone
    rep2 = importer.import_txdmv(db, ROWS, counties=["Fort Bend"], cities=[])
    assert rep2.created == 0 and rep2.merged == 3


def test_franchise_can_be_included_explicitly(db):
    rep = importer.import_txdmv(db, ROWS, counties=[], cities=["Sugar Land"], include_franchise=True)
    assert rep.created == 2
    assert db.query(Prospect).filter(Prospect.dealership_type == "franchise").count() == 1


def test_unverified_inference_is_flagged_in_score(db):
    from dealerflow.services.scoring import score_prospect

    importer.import_txdmv(db, ROWS, counties=["Fort Bend"], cities=[])
    p = db.query(Prospect).filter(Prospect.name == "Sim Motors (SIMULATED)").one()
    b = score_prospect(db, p)
    assert "dealership type not verified by a person" in b["unverified"]
    assert db.query(Evidence).filter(Evidence.field == "license_email").count() == 1


def test_txdmv_import_through_the_web_form(db, client):
    import io

    from openpyxl import Workbook

    admin = make_user(db)
    csrf = login(client, admin.email)
    wb = Workbook()
    ws = wb.active
    ws.append(COLS)
    for r in ROWS:
        ws.append([r[c] for c in COLS])
    buf = io.BytesIO()
    wb.save(buf)
    files = {"file": ("dealers.xlsx", buf.getvalue())}
    r = client.post("/import", data={"csrf": csrf, "counties": "Fort Bend"}, files=files)
    assert "Preview: 3 new" in r.text and "franchise dealer (1)" in r.text
    assert db.query(Prospect).count() == 0
    r = client.post("/import", data={"csrf": csrf}, files={"file": ("dealers.xlsx", buf.getvalue())})
    assert "Choose at least one county or city" in r.text
