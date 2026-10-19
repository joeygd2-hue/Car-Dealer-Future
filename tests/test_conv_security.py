"""Customer-conversation authorization over HTTP: tenant isolation, CSRF, roles, public chat API, ADF, pilot console.

Adapted from Phase 3's portal-security and role-matrix tests to the unified workspace URLs (/dealers/{id}/...) and
roles: dealer_admin, dealer_reviewer and dealer_viewer for dealership users; viewer/rep/manager/admin for agency staff.
"""

import pytest
from sqlalchemy import select

from dealerflow.db import session_scope
from dealerflow.models import Conversation, ModeAuthorization, ResponseAutomationSettings, ResponseDraft, User
from dealerflow.security import hash_password
from tests.conv_helpers import PW, chat, login, run_worker


def _conv(world, key="b", visitor="sec-1"):
    _, cid, _ = chat(world[key]["d"], visitor, "How much is the Civic?")
    run_worker()
    return cid


def _settings(client_id):
    with session_scope() as s:
        row = s.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == client_id))
        return {"paused": row.paused, "mode": row.mode}


def test_login_required(client, world):
    r = client.get(f"/dealers/{world['a']['d'].id}/inbox", follow_redirects=False)
    assert r.status_code in (303, 401) and (r.status_code == 401 or "/login" in r.headers["location"])
    assert client.get(f"/api/v1/dealers/{world['a']['d'].id}/conversations").status_code == 401


def test_cross_client_read_and_write_blocked(client, world):
    cid_b = _conv(world)
    csrf = login(client, "staff@a.example")
    a_id, b_id = world["a"]["d"].id, world["b"]["d"].id
    assert client.get(f"/dealers/{a_id}/inbox").status_code == 200
    assert client.get(f"/dealers/{b_id}/inbox").status_code in (403, 404)
    assert client.get(f"/dealers/{b_id}/conversations/{cid_b}").status_code in (403, 404)
    # B's conversation through A's URL is refused by the object ownership check.
    assert client.get(f"/dealers/{a_id}/conversations/{cid_b}").status_code == 404
    assert client.post(f"/dealers/{a_id}/conversations/{cid_b}/send", data={"body": "hi", "csrf": csrf}).status_code == 404
    assert client.post(f"/dealers/{b_id}/replies/pause", data={"on": 1, "csrf": csrf}).status_code in (403, 404)
    assert _settings(b_id)["paused"] is False


def test_csrf_required_for_posts(client, world):
    cid = _conv(world, "a", "sec-2")
    login(client, "staff@a.example")
    url = f"/dealers/{world['a']['d'].id}/conversations/{cid}/takeover"
    assert client.post(url, data={"on": 1}).status_code == 403
    assert client.post(url, data={"on": 1, "csrf": "forged"}).status_code == 403
    with session_scope() as s:
        assert s.get(Conversation, cid).human_takeover is False


def test_assignment_limited_to_own_dealership_members(client, world):
    cid = _conv(world, "a", "sec-3")
    csrf = login(client, "admin@a.example")
    url = f"/dealers/{world['a']['d'].id}/conversations/{cid}/assign"
    client.post(url, data={"user_id": world["b"]["staff"].id, "csrf": csrf})
    with session_scope() as s:
        assert s.get(Conversation, cid).assigned_user_id is None
    assert client.post(url, data={"user_id": world["a"]["staff"].id, "csrf": csrf}, follow_redirects=False).status_code == 303
    with session_scope() as s:
        assert s.get(Conversation, cid).assigned_user_id == world["a"]["staff"].id


def test_reviewer_can_pause_but_not_resume_or_change_mode(client, world):
    csrf = login(client, "staff@a.example")
    d = world["a"]["d"].id
    assert client.post(f"/dealers/{d}/replies/pause", data={"on": 1, "csrf": csrf}, follow_redirects=False).status_code == 303
    assert _settings(d)["paused"] is True
    assert client.post(f"/dealers/{d}/replies/pause", data={"on": 0, "csrf": csrf}).status_code == 403
    assert client.post(f"/dealers/{d}/replies/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on",
                                                            "csrf": csrf}).status_code == 403
    assert _settings(d) == {"paused": True, "mode": "A_DRAFT_ONLY"}


def test_mode_b_requires_dealership_admin_passing_eval_and_ack(client, db, world):
    d = world["a"]["d"].id
    # Agency staff can run the evaluation but cannot authorize automation on the dealership's behalf.
    db.add(User(email="mgr@agency.example", name="mgr", role="manager", password_hash=hash_password(PW)))
    db.commit()
    csrf = login(client, "mgr@agency.example")
    client.post(f"/dealers/{d}/replies/evaluate", data={"csrf": csrf})
    assert client.post(f"/dealers/{d}/replies/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on",
                                                            "csrf": csrf}).status_code == 403
    client.cookies.clear()
    csrf = login(client, "admin@a.example")
    client.post(f"/dealers/{d}/replies/mode", data={"mode": "B_FAQ_AUTOMATION", "csrf": csrf})
    assert _settings(d)["mode"] == "A_DRAFT_ONLY"  # missing the explicit authorization checkbox
    client.post(f"/dealers/{d}/replies/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf})
    assert _settings(d)["mode"] == "B_FAQ_AUTOMATION"
    with session_scope() as s:
        auth = s.scalar(select(ModeAuthorization).where(ModeAuthorization.client_id == d))
        assert auth.authorized_by_id == world["a"]["admin"].id and "admin@a.example" in auth.statement


def test_mode_b_without_eval_refused(client, world):
    d = world["a"]["d"].id
    csrf = login(client, "admin@a.example")
    client.post(f"/dealers/{d}/replies/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf})
    assert _settings(d)["mode"] == "A_DRAFT_ONLY"


ADMIN_ONLY_POSTS = [
    ("/replies/business", {"phone": "1"}),
    ("/replies/faqs", {"question": "q", "answer": "a"}),
    ("/replies/templates", {"intent": "price", "body": "x"}),
    ("/replies/limits", {"max_conv": 1, "max_dealer": 1, "min_interval": 5, "stale_hours": 24, "threshold": 0.9}),
    ("/replies/evaluate", {}),
    ("/replies/mode", {"mode": "A_DRAFT_ONLY"}),
    ("/replies/notifications", {}),
]


@pytest.mark.parametrize("path,data", ADMIN_ONLY_POSTS)
def test_reviewer_cannot_use_dealer_admin_actions(client, world, path, data):
    csrf = login(client, "staff@a.example")
    assert client.post(f"/dealers/{world['a']['d'].id}{path}", data={**data, "csrf": csrf}).status_code == 403, path


@pytest.mark.parametrize("path,data", ADMIN_ONLY_POSTS)
def test_other_dealer_admin_cannot_use_them_either(client, world, path, data):
    csrf = login(client, "admin@b.example")
    assert client.post(f"/dealers/{world['a']['d'].id}{path}", data={**data, "csrf": csrf}).status_code in (403, 404), path


def test_viewer_can_read_but_not_respond(client, db, world):
    cid = _conv(world, "a", "sec-v")
    db.add(User(email="viewer@a.example", name="v", role="client", client_id=world["a"]["d"].id, client_role="dealer_viewer",
                password_hash=hash_password(PW)))
    db.commit()
    csrf = login(client, "viewer@a.example")
    d = world["a"]["d"].id
    assert client.get(f"/dealers/{d}/conversations/{cid}").status_code == 200
    assert client.post(f"/dealers/{d}/conversations/{cid}/send", data={"body": "hi", "csrf": csrf}).status_code == 403
    assert client.post(f"/dealers/{d}/replies/pause", data={"on": 1, "csrf": csrf}).status_code == 403


def test_workspace_pages_render_and_draft_send(client, world):
    cid = _conv(world, "a", "sec-4")
    csrf = login(client, "admin@a.example")
    d = world["a"]["d"].id
    for path in ["/inbox", f"/conversations/{cid}", "/appointments", "/replies", "/notifications"]:
        assert client.get(f"/dealers/{d}{path}").status_code == 200, path
    with session_scope() as s:
        draft = s.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
        draft_id, body = draft.id, draft.body
    r = client.post(f"/dealers/{d}/conversations/{cid}/send", data={"body": body, "draft_id": draft_id, "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 303
    run_worker()
    with session_scope() as s:
        assert s.get(ResponseDraft, draft_id).status == "SENT"
        assert s.get(Conversation, cid).status == "AWAITING_CUSTOMER"


def test_agency_staff_pages_render(client, world):
    _conv(world, "a", "sec-5")
    login(client, "ops@platform.example")
    for path in ["/conversations/pilot", f"/dealers/{world['a']['d'].id}/inbox", f"/dealers/{world['b']['d'].id}/replies"]:
        assert client.get(path).status_code == 200, path


def test_pilot_console_forbidden_to_dealer_admin(client, world):
    csrf = login(client, "admin@a.example")
    assert client.get("/conversations/pilot").status_code == 403
    for path in ["/conversations/pilot/pause", "/conversations/pilot/recipients"]:
        assert client.post(path, data={"csrf": csrf, "on": 1, "kind": "EMAIL", "value": "a@b.c", "label": "x"}).status_code == 403


def test_marketplace_intake_human_assisted(client, world):
    csrf = login(client, "staff@a.example")
    d = world["a"]["d"].id
    data = {"thread_label": "Maria - 2019 Civic", "text": "¿Todavía está disponible el Civic?", "csrf": csrf}
    r1 = client.post(f"/dealers/{d}/marketplace/intake", data=data, follow_redirects=False)
    r2 = client.post(f"/dealers/{d}/marketplace/intake", data=data, follow_redirects=False)  # duplicate paste
    assert r1.status_code == r2.status_code == 303
    run_worker()
    with session_scope() as s:
        convs = list(s.scalars(select(Conversation).where(Conversation.channel == "MARKETPLACE_ASSISTED")))
        assert len(convs) == 1
        draft = s.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == convs[0].id))
        assert convs[0].preferred_language == "es" and "disponible" in draft.body
        assert any("manually" in r for r in draft.decision_reasons)


def test_website_chat_api_dedupe_rate_limit_and_unknown_key(client, world):
    key = world["a"]["d"].public_chat_key
    body = {"visitor_id": "visitor-xyz-1", "text": "Is the Civic available?", "client_message_id": "c1"}
    assert client.post(f"/api/chat/{key}/messages", json=body).status_code == 200
    assert client.post(f"/api/chat/{key}/messages", json=body).json()["duplicate"] is True
    codes = [client.post(f"/api/chat/{key}/messages", json={"visitor_id": "visitor-xyz-1", "text": f"m{i}",
                                                            "client_message_id": f"x{i}"}).status_code for i in range(10)]
    assert 429 in codes
    assert client.post("/api/chat/not-a-key/messages", json={"visitor_id": "v" * 10, "text": "hi"}).status_code == 404
    assert client.get(f"/api/chat/{key}/messages", params={"visitor_id": "visitor-xyz-1"}).json() == {"messages": []}


def test_website_chat_cors_limited_to_dealer_site(client, world):
    key = world["a"]["d"].public_chat_key
    ok = client.options(f"/api/chat/{key}/messages", headers={"Origin": "https://a.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://a.example"
    bad = client.options(f"/api/chat/{key}/messages", headers={"Origin": "https://evil.example"})
    assert bad.headers.get("access-control-allow-origin") != "https://evil.example"


def test_chat_visitor_cannot_read_another_dealerships_conversation(client, world):
    chat(world["b"]["d"], "shared-visitor", "Is the Civic available?")
    run_worker()
    key_a = world["a"]["d"].public_chat_key
    assert client.get(f"/api/chat/{key_a}/messages", params={"visitor_id": "shared-visitor"}).json() == {"messages": []}
