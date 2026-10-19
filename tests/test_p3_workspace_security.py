"""Phase 3 dealership workspace over HTTP: login, tenant isolation, CSRF, role matrix, Mode B/C authorization,
pages, marketplace intake, website-chat API."""

import pytest
from sqlalchemy import select

from dealerflow.db import session_scope
from dealerflow.models import User
from dealerflow.models_conversations import (
    Conversation,
    ConversationMessage,
    EvaluationRun,
    ModeAuthorization,
    ResponseAutomationSettings,
    ResponseDraft,
)
from tests.conftest import login as staff_login
from tests.dealer_helpers import dealer_login, page_csrf
from tests.phase3 import build_world, chat, run_worker


@pytest.fixture
def world(db):
    return build_world(db)


def _conv(world, key="b", visitor="sec-1"):
    _, cid, _ = chat(world[key]["d"], visitor, "How much is the Civic?")
    run_worker()
    return cid


def _as(client, world, email, key="a"):
    dealer_login(client, email)
    return page_csrf(client, f"/dealers/{world[key]['d'].id}/inbox")


def _settings(cid):
    with session_scope() as s:
        row = s.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == cid))
        return row.paused, row.mode


def test_login_required(client, world):
    r = client.get(f"/dealers/{world['a']['d'].id}/inbox", follow_redirects=False)
    assert r.status_code in (303, 401) and (r.status_code == 401 or "/login" in r.headers["location"])
    assert client.get(f"/api/v1/dealers/{world['a']['d'].id}/conversations").status_code == 401


def test_cross_client_read_and_write_blocked(client, world):
    cid_b = _conv(world)
    csrf = _as(client, world, "staff@a.example")
    a_id, b_id = world["a"]["d"].id, world["b"]["d"].id
    assert client.get(f"/dealers/{a_id}/inbox").status_code == 200
    assert client.get(f"/dealers/{b_id}/inbox").status_code == 404
    assert client.get(f"/dealers/{b_id}/conversations/{cid_b}").status_code == 404
    # B's conversation through A's URL is refused too (tenant-scoped lookup).
    assert client.get(f"/dealers/{a_id}/conversations/{cid_b}").status_code == 404
    # Form POSTs report dealership errors as a flash + redirect (Phase 2 convention); nothing may change.
    r = client.post(f"/dealers/{a_id}/conversations/{cid_b}/send", data={"body": "hi", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        assert s.scalar(select(ConversationMessage).where(ConversationMessage.conversation_id == cid_b,
                                                          ConversationMessage.direction == "OUTBOUND")) is None
    assert client.post(f"/dealers/{b_id}/replies/automation/pause", data={"on": 1, "csrf": csrf}).status_code == 404
    assert _settings(b_id)[0] is False


def test_csrf_required_for_posts(client, world):
    cid = _conv(world, "a", "sec-2")
    _as(client, world, "staff@a.example")
    url = f"/dealers/{world['a']['d'].id}/conversations/{cid}/takeover"
    assert client.post(url, data={"on": 1}).status_code == 403
    assert client.post(url, data={"on": 1, "csrf": "forged"}).status_code == 403


def test_assignment_limited_to_own_dealership(client, world):
    cid = _conv(world, "a", "sec-3")
    csrf = _as(client, world, "admin@a.example")
    url = f"/dealers/{world['a']['d'].id}/conversations/{cid}/assign"
    r = client.post(url, data={"user_id": world["b"]["staff"].id, "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303  # refused (flash + redirect)
    with session_scope() as s:
        assert s.get(Conversation, cid).assigned_user_id is None
    assert client.post(url, data={"user_id": world["a"]["staff"].id, "csrf": csrf}, follow_redirects=False).status_code == 303


def test_reviewer_can_pause_but_not_resume_or_change_mode(client, world):
    csrf = _as(client, world, "staff@a.example")
    d = world["a"]["d"].id
    assert client.post(f"/dealers/{d}/replies/automation/pause", data={"on": 1, "csrf": csrf},
                       follow_redirects=False).status_code == 303
    assert client.post(f"/dealers/{d}/replies/automation/pause", data={"on": 0, "csrf": csrf}).status_code == 403
    assert client.post(f"/dealers/{d}/replies/automation/mode",
                       data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf}).status_code == 403
    assert _settings(d) == (True, "A_DRAFT_ONLY")


def test_mode_b_requires_passing_eval_and_explicit_authorization(client, world):
    csrf = _as(client, world, "admin@a.example")
    d = world["a"]["d"].id
    mode = f"/dealers/{d}/replies/automation/mode"
    client.post(mode, data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf})
    assert _settings(d)[1] == "A_DRAFT_ONLY"  # no evaluation yet
    client.post(f"/dealers/{d}/replies/automation/evaluate", data={"csrf": csrf})
    with session_scope() as s:
        assert s.scalar(select(EvaluationRun).where(EvaluationRun.client_id == d)).passed
    client.post(mode, data={"mode": "B_FAQ_AUTOMATION", "csrf": csrf})
    assert _settings(d)[1] == "A_DRAFT_ONLY"  # missing the authorization checkbox
    client.post(mode, data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf})
    assert _settings(d)[1] == "B_FAQ_AUTOMATION"
    with session_scope() as s:
        auth = s.scalar(select(ModeAuthorization).where(ModeAuthorization.client_id == d))
        assert auth.authorized_by_id == world["a"]["admin"].id and "admin@a.example" in auth.statement


def test_agency_staff_cannot_authorize_automation_for_a_dealership(client, world):
    """Mode B/C must be authorized by one of the dealership's own administrators (owner decision)."""
    csrf = staff_login(client, "ops@platform.example")
    d = world["a"]["d"].id
    client.post(f"/dealers/{d}/replies/automation/evaluate", data={"csrf": csrf})
    r = client.post(f"/dealers/{d}/replies/automation/mode", data={"mode": "B_FAQ_AUTOMATION", "statement_ack": "on", "csrf": csrf})
    assert r.status_code == 403 and _settings(d)[1] == "A_DRAFT_ONLY"


ADMIN_ONLY_POSTS = [
    ("/replies/business", {"phone": "1"}),
    ("/replies/faqs", {"question": "q", "answer": "a"}),
    ("/replies/templates", {"intent": "price", "body": "x"}),
    ("/replies/automation/limits", {"max_conv": 1, "max_dealer": 1, "min_interval": 5, "stale_hours": 24, "threshold": 0.9}),
    ("/replies/automation/evaluate", {}),
    ("/replies/notifications", {}),
    ("/replies/channels", {"channel": "WEBSITE_CHAT"}),
]


@pytest.mark.parametrize("path,data", ADMIN_ONLY_POSTS)
def test_reviewer_cannot_use_dealer_admin_actions(client, world, path, data):
    csrf = _as(client, world, "staff@a.example")
    assert client.post(f"/dealers/{world['a']['d'].id}{path}", data={**data, "csrf": csrf}).status_code == 403, path


@pytest.mark.parametrize("path,data", ADMIN_ONLY_POSTS)
def test_other_dealer_admin_cannot_use_them_either(client, world, path, data):
    csrf = _as(client, world, "admin@b.example", "b")
    assert client.post(f"/dealers/{world['a']['d'].id}{path}", data={**data, "csrf": csrf}).status_code == 404, path


@pytest.mark.parametrize("path", ["/conversations/global-pause", "/conversations/evaluate"])
def test_agency_only_actions_forbidden_to_dealer_admin(client, world, path):
    csrf = _as(client, world, "admin@a.example")
    assert client.post(path, data={"csrf": csrf, "on": 1}).status_code == 403
    assert client.get("/conversations").status_code == 403


def test_dealer_pages_render_and_draft_send(client, world):
    cid = _conv(world, "a", "sec-4")
    csrf = _as(client, world, "admin@a.example")
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


def test_agency_pages_render(client, world):
    _conv(world, "a", "sec-5")
    staff_login(client, "ops@platform.example")
    d = world["a"]["d"].id
    for path in ["/conversations", f"/dealers/{d}/inbox", f"/dealers/{d}/replies", f"/dealers/{d}/appointments"]:
        assert client.get(path).status_code == 200, path


def test_viewer_cannot_respond(client, db, world):
    from dealerflow.security import hash_password
    from tests.phase3 import PASSWORD

    db.add(User(email="viewer@a.example", name="V", role="client", client_id=world["a"]["d"].id, client_role="dealer_viewer",
                password_hash=hash_password(PASSWORD)))
    db.commit()
    cid = _conv(world, "a", "sec-6")
    csrf = _as(client, world, "viewer@a.example")
    assert client.get(f"/dealers/{world['a']['d'].id}/conversations/{cid}").status_code == 200
    assert client.post(f"/dealers/{world['a']['d'].id}/conversations/{cid}/send",
                       data={"body": "hi", "csrf": csrf}).status_code == 403


def test_marketplace_intake_human_assisted(client, world):
    csrf = _as(client, world, "staff@a.example")
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


def test_website_chat_api_dedupe_rate_limit_and_privacy(client, db, world):
    from dealerflow.models import Client

    key = db.get(Client, world["a"]["d"].id).public_chat_key
    body = {"visitor_id": "visitor-xyz-1", "text": "Is the Civic available?", "client_message_id": "c1"}
    assert client.post(f"/api/chat/{key}/messages", json=body).status_code == 200
    assert client.post(f"/api/chat/{key}/messages", json=body).json()["duplicate"] is True
    codes = [client.post(f"/api/chat/{key}/messages", json={"visitor_id": "visitor-xyz-1", "text": f"m{i}",
                                                            "client_message_id": f"x{i}"}).status_code for i in range(10)]
    assert 429 in codes
    assert client.post("/api/chat/not-a-key/messages", json={"visitor_id": "v" * 10, "text": "hi"}).status_code == 404
    # Drafts are never shown to the visitor; only messages actually sent are.
    run_worker()
    assert client.get(f"/api/chat/{key}/messages", params={"visitor_id": "visitor-xyz-1"}).json() == {"messages": []}
    # Another visitor id cannot read this visitor's thread.
    assert client.get(f"/api/chat/{key}/messages", params={"visitor_id": "visitor-other"}).json() == {"messages": []}
