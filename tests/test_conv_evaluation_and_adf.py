import pytest
from sqlalchemy import select

from dealerflow.models import Conversation, IntegrationConnection
from dealerflow.services.conversations.adf import parse_adf
from dealerflow.services.conversations.evaluation import THRESHOLDS, run_cases
from tests.conv_helpers import run_worker

ADF = """<?xml version="1.0"?><adf><prospect><id>LEAD-77</id><requestdate>2026-10-10T15:00:00Z</requestdate>
<vehicle><year>2019</year><make>Honda</make><model>Civic</model><vin>2HGFC2F70KH500001</vin></vehicle>
<customer><contact><name part="first">Ana</name><name part="last">Lopez</name><email>ana@example.com</email><phone>8325550111</phone></contact>
<comments>Is this still available?</comments></customer><provider><name>ListingSite</name></provider></prospect></adf>"""


def test_synthetic_evaluation_meets_thresholds():
    out = run_cases()
    assert out["total"] >= 40
    for k, t in THRESHOLDS.items():
        assert out["metrics"][k] >= t, (k, out["metrics"][k], out["failures"])
    assert out["passed"]


def test_adf_parse():
    lead = parse_adf(ADF)[0]
    assert lead.lead_id == "LEAD-77" and lead.name == "Ana Lopez" and lead.vin == "2HGFC2F70KH500001"


def test_adf_rejects_entity_expansion():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><adf><prospect><id>&b;</id></prospect></adf>'
    from defusedxml import DTDForbidden, EntitiesForbidden

    with pytest.raises((EntitiesForbidden, DTDForbidden)):
        parse_adf(evil)


def test_adf_endpoint_auth_dedupe_and_vehicle_link(db, world, client):
    import hashlib

    conn = IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="ADF_LEAD", status="sandbox", external_account_id="adf:a",
                                 config={"token_sha256": hashlib.sha256(b"secret-token").hexdigest()})
    db.add(conn)
    db.commit()
    assert client.post(f"/api/leads/adf/{conn.id}", content=ADF, headers={"authorization": "Bearer wrong"}).status_code == 401
    for _ in range(2):
        r = client.post(f"/api/leads/adf/{conn.id}", content=ADF, headers={"authorization": "Bearer secret-token"})
        assert r.status_code == 200
    run_worker()
    convs = list(db.scalars(select(Conversation).where(Conversation.channel == "ADF_LEAD")))
    assert len(convs) == 1 and convs[0].vehicle_id == world["a"]["civic"].id


def test_adf_endpoint_refuses_disabled_connection(db, world, client):
    import hashlib

    conn = IntegrationConnection(client_id=world["a"]["d"].id, kind="customer_messaging", provider="ADF_LEAD", status="disabled",
                                 external_account_id="adf:a2", config={"token_sha256": hashlib.sha256(b"tok2").hexdigest()})
    db.add(conn)
    db.commit()
    assert client.post(f"/api/leads/adf/{conn.id}", content=ADF, headers={"authorization": "Bearer tok2"}).status_code == 403
    assert db.scalar(select(Conversation).where(Conversation.channel == "ADF_LEAD")) is None
