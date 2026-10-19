"""Role matrix over HTTP: agency staff vs dealership admin / reviewer / viewer on state-changing endpoints (ported from
app/tests/test_role_matrix.py). Old /portal/{id}/settings/... routes are /dealers/{client_id}/responses|automation|
notifications/...; old DEALER_STAFF is ``dealer_reviewer``; old platform-only /admin/... actions are /admin/pilot/...,
/agent/..., /settings/... and /clients/... (agency staff only). Synthetic data only.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from dealerflow.models import AgentState, PilotRecipient, User
from dealerflow.models_customer import CustomerMessage, ModeAuthorization, ResponseAutomationSettings
from tests.conftest import csrf_of, login
from tests.customer_helpers import chat, dealer_login, run_worker, world  # noqa: F401

# Dealership-admin ("manage") actions in the dealership workspace.
MANAGE_POSTS = [
    ("/responses/business", {"phone": "1"}),
    ("/responses/faqs", {"question": "q", "answer": "a"}),
    ("/responses/templates", {"intent": "price", "body": "x"}),
    ("/automation/limits", {"max_conv": 1, "max_dealer": 1, "min_interval": 5, "stale_hours": 24, "threshold": 0.9}),
    ("/automation/evaluate", {}),
    ("/automation/chat-key", {}),
    ("/notifications", {}),
]


def _settings_row(db, client_id):
    db.expire_all()
    return db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == client_id))


@pytest.mark.parametrize("path,data", MANAGE_POSTS)
def test_reviewer_cannot_use_dealer_admin_actions(client, world, path, data):
    csrf = dealer_login(client, "reviewer@a.example")
    r = client.post(f"/dealers/{world['a']['id']}{path}", data={**data, "csrf": csrf})
    assert r.status_code == 403, path


@pytest.mark.parametrize("path,data", MANAGE_POSTS)
def test_own_dealer_admin_can_use_them(client, world, path, data):
    """Control for the two tests around it: same request, same CSRF handling, permitted role → accepted."""
    csrf = dealer_login(client, "admin@a.example")
    r = client.post(f"/dealers/{world['a']['id']}{path}", data={**data, "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303, path


@pytest.mark.parametrize("path,data", MANAGE_POSTS + [("/automation/mode", {"mode": "A_DRAFT_ONLY"})])
def test_other_dealership_admin_cannot_use_them_either(client, db, world, path, data):
    key_before = _settings_row(db, world["a"]["id"]).chat_widget_key
    csrf = dealer_login(client, "admin@b.example")
    r = client.post(f"/dealers/{world['a']['id']}{path}", data={**data, "csrf": csrf})
    assert r.status_code == 404, path
    assert _settings_row(db, world["a"]["id"]).chat_widget_key == key_before


@pytest.mark.parametrize("staff_email", ["admin@agency.example", "manager@agency.example"])
@pytest.mark.parametrize("mode", ["B_FAQ_AUTOMATION", "C_CONTROLLED"])
def test_agency_staff_cannot_authorize_automated_modes(client, db, world, staff_email, mode):
    a = world["a"]["id"]
    login(client, staff_email)
    # A passing evaluation exists, so the only thing in the way is who is asking.
    r = client.post(f"/dealers/{a}/automation/evaluate", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/dealers/{a}/automation/mode", data={"mode": mode, "statement_ack": "on", "csrf": csrf_of(client)})
    assert r.status_code == 403
    assert _settings_row(db, a).mode == "A_DRAFT_ONLY"
    assert db.scalar(select(ModeAuthorization)) is None
    # Going back to draft-only (the safe direction) is allowed.
    r = client.post(f"/dealers/{a}/automation/mode", data={"mode": "A_DRAFT_ONLY", "csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.parametrize("email", ["admin@a.example", "reviewer@a.example", "viewer@a.example"])
def test_dealer_users_get_403_on_agency_pages(client, world, email):
    dealer_login(client, email)
    for path in ("/admin/pilot", "/settings", "/agent", "/clients", "/outreach", "/audit"):
        assert client.get(path, follow_redirects=False).status_code == 403, (email, path)


@pytest.mark.parametrize("path", ["/admin/pilot/emergency-stop", "/admin/pilot/global-pause", "/admin/pilot/return-to-sandbox",
                                  "/admin/pilot/allowlist", "/agent/live", "/agent/status", "/settings/user",
                                  "/settings/users/1/reset-link", "/clients"])
def test_agency_only_actions_forbidden_to_dealer_admin(client, db, world, path):
    csrf = dealer_login(client, "admin@a.example")
    r = client.post(path, data={"csrf": csrf, "on": 1, "reason": "x", "kind": "EMAIL", "value": "x@y.example", "label": "nope",
                                "status": "emergency_stopped", "authorize": "1", "confirm": "AUTHORIZE LIVE", "email": "x@y.example",
                                "role": "admin", "active": "1", "name": "x"})
    assert r.status_code == 403, path
    db.expire_all()
    st = db.get(AgentState, 1)
    assert st is None or (st.status != "emergency_stopped" and not st.live_authorized)
    assert db.scalar(select(PilotRecipient)) is None
    assert db.scalar(select(User).where(User.email == "x@y.example")) is None


def test_viewer_cannot_respond_but_reviewer_can(client, db, world):
    a = world["a"]["id"]
    _, conv_id, _ = chat(a, "role-visitor-1", "Is the Civic available?")
    run_worker()
    csrf = dealer_login(client, "viewer@a.example")
    assert client.get(f"/dealers/{a}/inbox/{conv_id}").status_code == 200  # can read
    r = client.post(f"/dealers/{a}/inbox/{conv_id}/send", data={"body": "Yes, it is available.", "csrf": csrf})
    assert r.status_code == 403
    db.expire_all()
    assert db.scalar(select(CustomerMessage).where(CustomerMessage.direction == "OUTBOUND", CustomerMessage.sender_type == "STAFF")) is None
    assert client.post(f"/dealers/{a}/automation/pause", data={"on": 1, "csrf": csrf}).status_code == 403
    assert not _settings_row(db, a).paused

    client.cookies.clear()
    csrf = dealer_login(client, "reviewer@a.example")
    r = client.post(f"/dealers/{a}/inbox/{conv_id}/send", data={"body": "Yes, it is available.", "csrf": csrf,
                                                                 "acknowledge_warnings": "on"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    sent = db.scalar(select(CustomerMessage).where(CustomerMessage.direction == "OUTBOUND", CustomerMessage.sender_type == "STAFF"))
    assert sent is not None and sent.body == "Yes, it is available." and sent.conversation_id == conv_id
