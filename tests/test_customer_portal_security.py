"""Server-side authorization, tenant isolation, CSRF, roles — exercised over HTTP.

Ported from app/tests/test_portal_security.py. Old /portal/{id}/... routes are /dealers/{client_id}/... in
dealerflow; old DEALER_STAFF is ``dealer_reviewer``; the old platform-admin workspace (/admin) is agency staff's
Phase 1 control pages (/agent, /outreach, /audit). dealerflow reports form outcomes as a flash message on the page the
redirect leads to (old: ``err=`` / ``msg=`` query parameters). Synthetic data only.
"""

from __future__ import annotations

from sqlalchemy import select

from dealerflow.db import session_factory
from dealerflow.models_customer import (
    CustomerConversation,
    ResponseAutomationSettings,
    ResponseDraft,
)
from tests.conftest import login
from tests.customer_helpers import (  # noqa: F401
    PASSWORD,
    chat,
    csrf_from,
    dealer_login,
    run_worker,
    world,
)


def _conv(world, key="b", visitor="sec-1"):
    _, cid, _ = chat(world[key]["id"], visitor, "How much is the Civic?")
    run_worker()
    return cid


def _flash_after(client, response) -> str:
    """Follow a 303 and return the flash banner shown on the resulting page."""
    assert response.status_code == 303, response.text[:300]
    page = client.get(response.headers["location"]).text
    assert '<div class="flash' in page, page[:500]
    return page.split('<div class="flash', 1)[1].split("</div>", 1)[0]


def test_login_required(client, world):
    r = client.get(f"/dealers/{world['a']['id']}", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    csrf = csrf_from(client, "/login")
    r = client.post("/login", data={"email": "admin@a.example", "password": "wrong-password", "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 200
    # Still not authenticated after a failed login.
    r = client.get(f"/dealers/{world['a']['id']}", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_cross_client_read_and_write_blocked(client, world):
    cid_b = _conv(world)
    csrf = dealer_login(client, "reviewer@a.example")
    a_id, b_id = world["a"]["id"], world["b"]["id"]
    assert client.get(f"/dealers/{a_id}").status_code == 200
    assert client.get(f"/dealers/{b_id}").status_code == 404
    assert client.get(f"/dealers/{b_id}/inbox").status_code == 404
    assert client.get(f"/dealers/{b_id}/inbox/{cid_b}").status_code == 404
    # Accessing B's conversation through A's URL is also refused (object ownership check).
    assert client.get(f"/dealers/{a_id}/inbox/{cid_b}").status_code == 404
    r = client.post(f"/dealers/{a_id}/inbox/{cid_b}/send", data={"body": "hi", "csrf": csrf})
    assert r.status_code == 404
    r = client.post(f"/dealers/{b_id}/automation/pause", data={"on": 1, "csrf": csrf})
    assert r.status_code == 404
    s = session_factory()()
    try:
        assert s.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == b_id)).paused is False
    finally:
        s.close()


def test_csrf_required_for_posts(client, world):
    cid = _conv(world, "a", "sec-2")
    dealer_login(client, "reviewer@a.example")
    r = client.post(f"/dealers/{world['a']['id']}/inbox/{cid}/takeover", data={"on": 1})
    assert r.status_code == 403
    r = client.post(f"/dealers/{world['a']['id']}/inbox/{cid}/takeover", data={"on": 1, "csrf": "forged"})
    assert r.status_code == 403


def test_assignment_limited_to_own_staff(client, world):
    cid = _conv(world, "a", "sec-3")
    csrf = dealer_login(client, "admin@a.example")
    r = client.post(f"/dealers/{world['a']['id']}/inbox/{cid}/assign",
                    data={"user_id": world["b"]["reviewer"], "csrf": csrf})
    assert r.status_code == 400
    r = client.post(f"/dealers/{world['a']['id']}/inbox/{cid}/assign",
                    data={"user_id": world["a"]["reviewer"], "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303


def test_staff_can_pause_but_not_change_mode_or_resume(client, world):
    csrf = dealer_login(client, "reviewer@a.example")
    d = world["a"]["id"]
    assert client.post(f"/dealers/{d}/automation/pause", data={"on": 1, "csrf": csrf},
                       follow_redirects=False).status_code == 303
    assert client.post(f"/dealers/{d}/automation/pause", data={"on": 0, "csrf": csrf}).status_code == 403
    assert client.post(f"/dealers/{d}/automation/mode",
                       data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf}).status_code == 403


def test_mode_b_requires_passing_eval_and_ack(client, world):
    csrf = dealer_login(client, "admin@a.example")
    d = world["a"]["id"]
    r = client.post(f"/dealers/{d}/automation/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf},
                    follow_redirects=False)
    assert _flash_after(client, r).startswith(' error">')
    r = client.post(f"/dealers/{d}/automation/evaluate", data={"csrf": csrf}, follow_redirects=False)
    assert "PASSED" in _flash_after(client, r)
    r = client.post(f"/dealers/{d}/automation/mode", data={"mode": "B_FAQ_AUTOMATION", "csrf": csrf}, follow_redirects=False)
    assert _flash_after(client, r).startswith(' error">')  # missing explicit authorization checkbox
    r = client.post(f"/dealers/{d}/automation/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf},
                    follow_redirects=False)
    banner = _flash_after(client, r)
    assert not banner.startswith(' error">') and "Mode set to B_FAQ_AUTOMATION" in banner


def test_admin_workspace_forbidden_to_dealers(client, world):
    csrf = dealer_login(client, "admin@a.example")
    assert client.get("/agent").status_code == 403
    assert client.post("/agent/status", data={"status": "emergency_stopped", "csrf": csrf}).status_code == 403
    assert client.get("/outreach").status_code == 403


def test_full_portal_pages_render(client, world):
    cid = _conv(world, "a", "sec-4")
    csrf = dealer_login(client, "admin@a.example")
    d = world["a"]["id"]
    for path in ["", "/inbox", f"/inbox/{cid}", "/listings", "/appointments", "/responses", "/automation",
                 "/notifications", "/settings", "/reports"]:
        assert client.get(f"/dealers/{d}{path}").status_code == 200, path
    s = session_factory()()
    try:
        draft = s.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == cid))
    finally:
        s.close()
    r = client.post(f"/dealers/{d}/inbox/{cid}/send", data={"body": draft.body, "draft_id": draft.id, "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 303
    run_worker()
    s = session_factory()()
    try:
        assert s.get(ResponseDraft, draft.id).status == "SENT"
        assert s.get(CustomerConversation, cid).status == "AWAITING_CUSTOMER"
    finally:
        s.close()


def test_platform_admin_pages_render(client, world):
    login(client, "admin@agency.example", PASSWORD)
    for path in ["/agent", f"/dealers/{world['a']['id']}", f"/dealers/{world['a']['id']}/automation", "/audit", "/outreach"]:
        assert client.get(path).status_code == 200, path


def test_marketplace_intake_human_assisted(client, world):
    csrf = dealer_login(client, "reviewer@a.example")
    d = world["a"]["id"]
    data = {"thread_label": "Maria - 2019 Civic", "text": "¿Todavía está disponible el Civic?", "csrf": csrf}
    r1 = client.post(f"/dealers/{d}/inbox/marketplace", data=data, follow_redirects=False)
    r2 = client.post(f"/dealers/{d}/inbox/marketplace", data=data, follow_redirects=False)  # duplicate paste
    assert r1.status_code == r2.status_code == 303
    assert "already" in _flash_after(client, r2)
    run_worker()
    s = session_factory()()
    try:
        convs = list(s.scalars(select(CustomerConversation).where(CustomerConversation.channel == "MARKETPLACE_ASSISTED")))
        assert len(convs) == 1
        conv = convs[0]
        draft = s.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id))
        assert conv.preferred_language == "es" and "disponible" in draft.body
        assert any("manually" in r for r in draft.decision_reasons)
    finally:
        s.close()


def test_website_chat_api_and_rate_limit(client, world, monkeypatch):
    from dealerflow.config import get_settings

    # Old build: per-visitor limit hard-coded at 8/min. dealerflow makes it configurable; set it to the old value.
    monkeypatch.setenv("DF_CHAT_MAX_MESSAGES_PER_VISITOR_PER_HOUR", "8")
    get_settings.cache_clear()
    s = session_factory()()
    try:
        key = s.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == world["a"]["id"])).chat_widget_key
    finally:
        s.close()
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
    get_settings.cache_clear()
