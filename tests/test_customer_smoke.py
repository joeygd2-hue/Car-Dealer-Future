"""Smoke test for the ported Phase 3 pipeline inside dealerflow (inbox → draft → staff send → sandbox delivery)."""

from __future__ import annotations

from sqlalchemy import select

from tests.conftest import login
from tests.customer_helpers import chat, dealer_login, run_worker, world  # noqa: F401


def test_chat_message_is_drafted_and_staff_reply_is_simulated(client, world):
    from dealerflow.db import session_factory
    from dealerflow.models_customer import CustomerConversation, CustomerMessage, ResponseDraft

    a = world["a"]
    _, conv_id, created = chat(a["id"], "visitor-0001", "Is the 2019 Civic still available? What's the price?")
    assert created
    run_worker()
    s = session_factory()()
    draft = s.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == conv_id))
    assert draft is not None and draft.status == "PENDING_REVIEW" and "$18,995" in draft.body
    assert draft.decision == "DRAFT_ONLY"
    s.close()

    csrf = dealer_login(client, "reviewer@a.example")
    page = client.get(f"/dealers/{a['id']}/inbox/{conv_id}")
    assert page.status_code == 200 and "$18,995" in page.text
    r = client.post(f"/dealers/{a['id']}/inbox/{conv_id}/send", data={"body": draft.body, "draft_id": draft.id, "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 303
    run_worker()
    s = session_factory()()
    out = s.scalar(select(CustomerMessage).where(CustomerMessage.conversation_id == conv_id, CustomerMessage.direction == "OUTBOUND"))
    assert out.delivery_status == "SIMULATED" and "sandbox" in (out.delivery_error or "")
    assert s.get(CustomerConversation, conv_id).first_response_at is not None
    s.close()
    # Bravo's admin cannot see Alpha's conversation.
    client.cookies.clear()
    dealer_login(client, "admin@b.example")
    assert client.get(f"/dealers/{a['id']}/inbox/{conv_id}").status_code == 404
    assert client.get(f"/dealers/{world['b']['id']}/inbox/{conv_id}").status_code == 404


def test_every_customer_page_renders_for_each_role(client, world):
    a = world["a"]
    _, conv_id, _ = chat(a["id"], "visitor-0002", "Hola, ¿tienen el Civic? Quiero una prueba de manejo el sábado a las 10am")
    run_worker()
    pages = ["inbox", f"inbox/{conv_id}", "appointments", "responses", "automation", "notifications"]
    for email in ("admin@a.example", "reviewer@a.example", "viewer@a.example"):
        client.cookies.clear()
        dealer_login(client, email)
        for p in pages:
            r = client.get(f"/dealers/{a['id']}/{p}")
            assert r.status_code == 200, (email, p, r.text[:300])
    client.cookies.clear()
    login(client, "viewer@agency.example")
    for p in pages:
        assert client.get(f"/dealers/{a['id']}/{p}").status_code == 200, p
