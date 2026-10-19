"""Phase 3 pages render for each role and the review → edit → approve flow works through the HTML forms."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dealerflow.web.app import create_app
from tests.conftest import login, make_user
from tests.conv_helpers import chat, only_conversation, outbound, pending_draft, setup_dealer
from tests.dealer_helpers import dealer_login, drain, page_csrf


def test_dealer_pages_render_for_each_role(client, db):
    d = setup_dealer()
    chat(client, d["key"], "Can I test drive the Accord tomorrow at 2pm?", name="Kim")
    drain()
    conv = only_conversation(d["id"])
    pages = [f"/dealers/{d['id']}/inbox", f"/dealers/{d['id']}/conversations/{conv.id}", f"/dealers/{d['id']}/appointments",
             f"/dealers/{d['id']}/responses", f"/chat/{d['key']}"]
    for who in ("admin", "viewer"):
        browser = TestClient(create_app())
        dealer_login(browser, f"{who}@{d['slug']}.example")
        for p in pages:
            r = browser.get(p)
            assert r.status_code == 200, (who, p, r.text[:200])
        convo = browser.get(pages[1]).text
        assert "DRAFT, not sent" in convo and "SIMULATED" in convo
        assert ("Approve and send" in convo) == (who == "admin")  # viewers see no reply form
        assert ("Save mode" in browser.get(pages[3]).text) == (who == "admin")
    make_user(db, "staff@agency.example", role="rep")
    login(client, "staff@agency.example")
    assert client.get(pages[0]).status_code == 200 and "Customer inbox" in client.get(pages[0]).text


def test_review_edit_approve_through_the_forms(client):
    d = setup_dealer()
    chat(client, d["key"], "Is the Accord available?")
    drain()
    conv = only_conversation(d["id"])
    draft = pending_draft(conv.id)
    browser = TestClient(create_app())
    dealer_login(browser, f"reviewer@{d['slug']}.example")
    url = f"/dealers/{d['id']}/conversations/{conv.id}"
    csrf = page_csrf(browser, url)
    # Unsupported claim → the page asks for acknowledgement and nothing is queued.
    r = browser.post(f"{url}/send", data={"csrf": csrf, "body": draft.body + " It has no accidents.", "draft_id": str(draft.id)})
    assert r.status_code == 200 and "Check before sending" in r.text and outbound(conv.id) == []
    r = browser.post(f"{url}/send", data={"csrf": csrf, "body": draft.body + " See you soon!", "draft_id": str(draft.id)},
                     follow_redirects=False)
    assert r.status_code == 303
    drain()
    [m] = outbound(conv.id)
    assert m.body.endswith("See you soon!") and m.delivery_status == "simulated"
    assert "simulated" in browser.get(url).text
    # Without the CSRF token the form is refused.
    assert browser.post(f"{url}/close", data={}).status_code == 403
