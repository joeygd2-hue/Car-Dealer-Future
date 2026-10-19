"""Phase 3 security: cross-dealership isolation (pages, API, ids), server-side roles, public endpoint abuse."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from dealerflow.db import session_scope
from dealerflow.models import Conversation, CustomerLead, User
from tests.conv_helpers import chat, only_conversation, pending_draft, setup_dealer, user_with_key
from tests.dealer_helpers import drain

ADF = """<?xml version="1.0"?><adf><prospect><id sequence="1" source="cars.example">L-1001</id><requestdate>2026-10-18T10:00:00</requestdate>
<vehicle><vin>1HGCM82633A004352</vin><year>2003</year><make>Honda</make><model>Accord</model></vehicle>
<customer><contact><name part="full">Robin Lead</name><email>robin@lead.example</email><phone>281-555-0177</phone></contact>
<comments>Is this Accord still available?</comments></customer><provider><name>cars.example</name></provider></prospect></adf>"""


@pytest.fixture
def two(client):
    a, b = setup_dealer("Alpha Auto"), setup_dealer("Bravo Cars")
    chat(client, a["key"], "Is the Accord available?")
    drain()
    conv = only_conversation(a["id"])
    return a, b, conv, pending_draft(conv.id)


def test_other_dealership_cannot_reach_a_conversation(client, two):
    a, b, conv, draft = two
    hb = b["h"]["admin"]
    base_b = f"/api/v1/dealers/{b['id']}"
    assert client.get(f"{base_b}/conversations/{conv.id}", headers=hb).status_code == 404  # id of A under B's workspace
    assert client.post(f"{base_b}/conversations/{conv.id}/reply", headers=hb, json={"body": "hi", "draft_id": draft.id}).status_code == 404
    assert client.get(f"{base_b}/conversations", headers=hb).json() == []
    # B's users cannot open A's workspace at all.
    assert client.get(f"/api/v1/dealers/{a['id']}/conversations", headers=hb).status_code == 404
    assert client.get(f"/dealers/{a['id']}/inbox", headers=hb).status_code == 404
    assert client.get(f"/dealers/{a['id']}/conversations/{conv.id}", headers=hb).status_code == 404
    # A draft from A cannot be sent through another conversation, even inside A.
    chat(client, a["key"], "Hours?", visitor="visitor-zzzz")
    drain()
    with session_scope() as db:
        other = db.scalar(select(Conversation).where(Conversation.client_id == a["id"], Conversation.id != conv.id))
    r = client.post(f"/api/v1/dealers/{a['id']}/conversations/{other.id}/reply", headers=a["h"]["admin"],
                    json={"body": "hi", "draft_id": draft.id})
    assert r.status_code == 404


def test_roles_are_enforced_on_the_server(client, two):
    a, b, conv, draft = two
    base = f"/api/v1/dealers/{a['id']}/conversations/{conv.id}"
    assert client.get(base, headers=a["h"]["viewer"]).status_code == 200  # viewers may read
    assert client.post(f"{base}/reply", headers=a["h"]["viewer"], json={"body": "hi"}).status_code == 403
    staff_viewer = user_with_key("viewer@agency.example", role="viewer")
    assert client.post(f"{base}/reply", headers=staff_viewer, json={"body": "hi"}).status_code == 403
    staff_rep = user_with_key("rep@agency.example", role="rep")
    assert client.post(f"{base}/reply", headers=staff_rep, json={"body": draft.body, "draft_id": draft.id}).json()["status"] == "queued"
    # Response settings need "manage": a reviewer cannot change the mode or business details.
    r = client.post(f"/dealers/{a['id']}/responses/mode", headers=a["h"]["reviewer"], data={"mode": "b_faq_automation"})
    assert r.status_code == 403
    # Dealership users never reach staff pages.
    assert client.get("/finance", headers=a["h"]["admin"]).status_code == 403


def test_cannot_assign_to_another_dealerships_user(client, two):
    a, b, conv, _ = two
    with session_scope() as db:
        bravo_user = db.scalar(select(User).where(User.client_id == b["id"]))
    r = client.post(f"/dealers/{a['id']}/conversations/{conv.id}/assign", headers=a["h"]["admin"],
                    data={"user_id": str(bravo_user.id)}, follow_redirects=False)
    assert r.status_code in (303, 400)
    assert only_conversation(a["id"]).assigned_user_id is None


def test_public_chat_ignores_other_dealers_vehicles_and_bad_keys(client, two):
    a, b, _, _ = two
    assert chat(client, "not-a-real-key", "hello").status_code == 404
    chat(client, b["key"], "Tell me about it", visitor="visitor-cross", vehicle_stock="A100")
    drain()
    with session_scope() as db:
        conv = db.scalar(select(Conversation).where(Conversation.client_id == b["id"]))
        from dealerflow.models_dealer import Vehicle

        if conv.vehicle_id:  # B also imported an A100 — it must be B's own vehicle, never A's
            assert db.get(Vehicle, conv.vehicle_id).client_id == b["id"]
    assert client.post(f"/api/public/chat/{a['key']}/messages", content=b"not json",
                       headers={"content-type": "application/json"}).status_code == 400
    assert chat(client, a["key"], "", visitor="visitor-empty").status_code == 400


def test_public_chat_is_rate_limited_across_processes(client):
    d = setup_dealer()
    codes = [chat(client, d["key"], f"message {i}", visitor="visitor-flood", msg_id=f"f{i}").status_code for i in range(10)]
    assert codes[:8] == [200] * 8 and codes[8] == 429  # 8 per visitor per minute (database counter)


def test_cors_only_for_the_dealership_site(client):
    d = setup_dealer()
    with session_scope() as db:
        from dealerflow.models_dealer import Client

        db.get(Client, d["id"]).website = "https://alpha.example"
    ok = chat(client, d["key"], "hi there", visitor="visitor-cors1")
    assert "access-control-allow-origin" not in ok.headers
    r = client.post(f"/api/public/chat/{d['key']}/messages", json={"visitor_id": "visitor-cors2", "text": "hi"},
                    headers={"origin": "https://alpha.example"})
    assert r.headers.get("access-control-allow-origin") == "https://alpha.example"
    r = client.post(f"/api/public/chat/{d['key']}/messages", json={"visitor_id": "visitor-cors3", "text": "hi"},
                    headers={"origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_adf_intake_creates_a_lead_once_and_refuses_hostile_xml(client):
    d = setup_dealer()
    assert client.post("/api/public/leads/adf/wrong-key", content=ADF).status_code == 404
    r = client.post(f"/api/public/leads/adf/{d['adf']}", content=ADF)
    assert r.json() == {"ok": True, "received": 1, "duplicates": 0}
    assert client.post(f"/api/public/leads/adf/{d['adf']}", content=ADF).json()["duplicates"] == 1
    drain()
    conv = only_conversation(d["id"])
    with session_scope() as db:
        lead = db.get(CustomerLead, conv.lead_id)
        assert conv.channel == "adf_lead" and lead.vehicle_id == conv.vehicle_id and lead.vehicle_id is not None
    assert pending_draft(conv.id).decision == "draft_only"  # ADF replies are sent by staff outside the platform
    xxe = """<?xml version="1.0"?><!DOCTYPE adf [<!ENTITY x SYSTEM "file:///etc/passwd">]><adf><prospect><id>X</id>
<customer><comments>&x;</comments></customer></prospect></adf>"""
    assert client.post(f"/api/public/leads/adf/{d['adf']}", content=xxe).status_code == 400
    assert client.post(f"/api/public/leads/adf/{d['adf']}", content=b"x" * (300 * 1024)).status_code == 413


def test_chat_page_has_no_inline_script(client):
    d = setup_dealer()
    r = client.get(f"/chat/{d['key']}")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]
    assert "<script>" not in r.text and 'src="/static/chat.js"' in r.text
