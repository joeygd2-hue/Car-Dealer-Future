"""Server-side authorization, tenant isolation, CSRF, roles — exercised over HTTP."""

from sqlalchemy import select

from app import db as dbmod
from app.models import AutomationSettings, Conversation, ResponseDraft
from tests.conftest import chat, login, run_worker


def _conv(world, key="b", visitor="sec-1"):
    _, cid, _ = chat(world[key]["d"], visitor, "How much is the Civic?")
    run_worker()
    return cid


def test_login_required(client, world):
    r = client.get(f"/portal/{world['a']['d'].id}", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert client.post("/login", data={"email": "admin@a.example", "password": "wrong-password"}).status_code == 200
    assert "cdf_session" not in client.cookies


def test_cross_client_read_and_write_blocked(client, world):
    cid_b = _conv(world)
    csrf = login(client, "staff@a.example")
    a_id, b_id = world["a"]["d"].id, world["b"]["d"].id
    assert client.get(f"/portal/{a_id}").status_code == 200
    assert client.get(f"/portal/{b_id}").status_code == 404
    assert client.get(f"/portal/{b_id}/inbox").status_code == 404
    assert client.get(f"/portal/{b_id}/conversations/{cid_b}").status_code == 404
    # Accessing B's conversation through A's URL is also refused (object ownership check).
    assert client.get(f"/portal/{a_id}/conversations/{cid_b}").status_code == 404
    r = client.post(f"/portal/{a_id}/conversations/{cid_b}/send", data={"body": "hi", "csrf_token": csrf})
    assert r.status_code == 404
    r = client.post(f"/portal/{b_id}/settings/automation/pause", data={"on": 1, "csrf_token": csrf})
    assert r.status_code == 404
    with dbmod.SessionLocal() as s:
        assert s.scalar(select(AutomationSettings).where(AutomationSettings.dealership_id == b_id)).paused is False


def test_csrf_required_for_posts(client, world):
    cid = _conv(world, "a", "sec-2")
    login(client, "staff@a.example")
    r = client.post(f"/portal/{world['a']['d'].id}/conversations/{cid}/takeover", data={"on": 1})
    assert r.status_code == 403
    r = client.post(f"/portal/{world['a']['d'].id}/conversations/{cid}/takeover", data={"on": 1, "csrf_token": "forged"})
    assert r.status_code == 403


def test_assignment_limited_to_own_staff(client, world):
    cid = _conv(world, "a", "sec-3")
    csrf = login(client, "admin@a.example")
    r = client.post(f"/portal/{world['a']['d'].id}/conversations/{cid}/assign",
                    data={"user_id": world["b"]["staff"].id, "csrf_token": csrf})
    assert r.status_code == 400
    r = client.post(f"/portal/{world['a']['d'].id}/conversations/{cid}/assign",
                    data={"user_id": world["a"]["staff"].id, "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303


def test_staff_can_pause_but_not_change_mode_or_resume(client, world):
    csrf = login(client, "staff@a.example")
    d = world["a"]["d"].id
    assert client.post(f"/portal/{d}/settings/automation/pause", data={"on": 1, "csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    assert client.post(f"/portal/{d}/settings/automation/pause", data={"on": 0, "csrf_token": csrf}).status_code == 403
    assert client.post(f"/portal/{d}/settings/automation/mode",
                       data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf_token": csrf}).status_code == 403


def test_mode_b_requires_passing_eval_and_ack(client, world):
    csrf = login(client, "admin@a.example")
    d = world["a"]["d"].id
    r = client.post(f"/portal/{d}/settings/automation/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on",
                                                                   "csrf_token": csrf}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post(f"/portal/{d}/settings/automation/evaluate", data={"csrf_token": csrf}, follow_redirects=False)
    assert "PASSED" in r.headers["location"]
    r = client.post(f"/portal/{d}/settings/automation/mode", data={"mode": "B_FAQ_AUTOMATION", "csrf_token": csrf},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]  # missing explicit authorization checkbox
    r = client.post(f"/portal/{d}/settings/automation/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on",
                                                                   "csrf_token": csrf}, follow_redirects=False)
    assert "msg=" in r.headers["location"]


def test_admin_workspace_forbidden_to_dealers(client, world):
    csrf = login(client, "admin@a.example")
    assert client.get("/admin").status_code == 403
    assert client.post("/admin/global-pause", data={"on": 1, "csrf_token": csrf}).status_code == 403
    assert client.get("/admin/outreach").status_code == 403


def test_full_portal_pages_render(client, world):
    cid = _conv(world, "a", "sec-4")
    csrf = login(client, "admin@a.example")
    d = world["a"]["d"].id
    for path in ["", "/inbox", f"/conversations/{cid}", "/inventory", "/appointments", "/settings/business",
                 "/settings/content", "/settings/automation", "/settings/notifications", "/notifications", "/audit",
                 "/account"]:
        assert client.get(f"/portal/{d}{path}").status_code == 200, path
    with dbmod.SessionLocal() as s:
        draft = s.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    r = client.post(f"/portal/{d}/conversations/{cid}/send", data={"body": draft.body, "draft_id": draft.id,
                                                                   "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    run_worker()
    with dbmod.SessionLocal() as s:
        assert s.get(ResponseDraft, draft.id).status == "SENT"
        assert s.get(Conversation, cid).status == "AWAITING_CUSTOMER"


def test_platform_admin_pages_render(client, world):
    login(client, "ops@platform.example")
    for path in ["/admin", f"/admin/dealerships/{world['a']['d'].id}", "/admin/audit", "/admin/outreach"]:
        assert client.get(path).status_code == 200, path


def test_marketplace_intake_human_assisted(client, world):
    csrf = login(client, "staff@a.example")
    d = world["a"]["d"].id
    data = {"thread_label": "Maria - 2019 Civic", "text": "¿Todavía está disponible el Civic?", "csrf_token": csrf}
    r1 = client.post(f"/portal/{d}/marketplace/intake", data=data, follow_redirects=False)
    r2 = client.post(f"/portal/{d}/marketplace/intake", data=data, follow_redirects=False)  # duplicate paste
    assert r1.status_code == r2.status_code == 303 and "already" in r2.headers["location"]
    run_worker()
    with dbmod.SessionLocal() as s:
        conv = s.scalar(select(Conversation).where(Conversation.channel == "MARKETPLACE_ASSISTED"))
        draft = s.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id))
        assert conv.preferred_language == "es" and "disponible" in draft.body
        assert any("manually" in r for r in draft.decision_reasons)


def test_website_chat_api_and_rate_limit(client, world):
    key = world["a"]["d"].public_chat_key
    r = client.post(f"/api/chat/{key}/messages", json={"visitor_id": "visitor-xyz-1", "text": "Is the Civic available?",
                                                       "client_message_id": "c1"})
    assert r.status_code == 200
    dup = client.post(f"/api/chat/{key}/messages", json={"visitor_id": "visitor-xyz-1", "text": "Is the Civic available?",
                                                         "client_message_id": "c1"})
    assert dup.json()["duplicate"] is True
    codes = [client.post(f"/api/chat/{key}/messages", json={"visitor_id": "visitor-xyz-1", "text": f"m{i}",
                                                            "client_message_id": f"x{i}"}).status_code for i in range(10)]
    assert 429 in codes
    assert client.post("/api/chat/not-a-key/messages", json={"visitor_id": "v" * 10, "text": "hi"}).status_code == 404
    assert client.get(f"/api/chat/{key}/messages", params={"visitor_id": "visitor-xyz-1"}).json() == {"messages": []}
