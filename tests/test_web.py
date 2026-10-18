"""Authentication, role-based access, CSRF, API keys and page smoke tests."""

from sqlalchemy import select

from dealerflow.models import AuditLog, OutreachMessage
from dealerflow.services.discovery import run_source
from tests.conftest import login, make_user

PAGES = ["/", "/agent", "/prospects", "/prospects/new", "/import", "/outreach", "/outreach?status=", "/inbox",
         "/meetings", "/tasks", "/reports/weekly", "/reports/weekly?simulated=1", "/integrations", "/settings",
         "/suppression", "/activity", "/audit"]


def test_anonymous_is_redirected_or_rejected(client):
    r = client.get("/prospects", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/api/v1/prospects").status_code == 401
    assert client.post("/agent/status", data={"status": "running"}).status_code in (401, 403, 303)


def test_login_rate_limit(db, client):
    make_user(db, "x@test.example", "admin")
    r = client.get("/login")
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]
    for _ in range(5):
        r = client.post("/login", data={"email": "x@test.example", "password": "wrong-password", "csrf": csrf})
        assert "Invalid email or password" in r.text
    r = client.post("/login", data={"email": "x@test.example", "password": "correct horse battery", "csrf": csrf})
    assert "Too many failed attempts" in r.text
    assert db.scalar(select(AuditLog).where(AuditLog.action == "auth.login_blocked"))


def test_all_pages_render_for_admin(db, client):
    admin = make_user(db)
    run_source(db, "fixture")
    db.commit()
    login(client, admin.email)
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        assert "SIMULATED providers active" in r.text
    pid = client.get("/api/v1/prospects").json()["items"][0]["id"]
    assert client.get(f"/prospects/{pid}").status_code == 200


def test_csrf_required_for_form_posts(db, client):
    admin = make_user(db)
    login(client, admin.email)
    assert client.post("/agent/status", data={"status": "running"}).status_code == 403
    assert client.post("/agent/status", data={"status": "running", "csrf": "wrong"}).status_code == 403


def test_viewer_and_rep_cannot_do_privileged_things(db, client):
    admin = make_user(db)
    make_user(db, "viewer@test.example", "viewer")
    make_user(db, "rep@test.example", "rep")
    run_source(db, "fixture")
    db.commit()
    csrf = login(client, "viewer@test.example")
    pid = client.get("/api/v1/prospects").json()["items"][0]["id"]
    assert client.post(f"/prospects/{pid}/note", data={"body": "x", "csrf": csrf}).status_code == 403
    assert client.post("/agent/status", data={"status": "running", "csrf": csrf}).status_code == 403
    client.post("/logout", data={"csrf": csrf})

    csrf = login(client, "rep@test.example")
    r = client.post(f"/prospects/{pid}/draft", data={"template_id": 1, "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    mid = db.scalar(select(OutreachMessage.id))
    assert client.post(f"/outreach/{mid}/approve", data={"csrf": csrf}).status_code == 403
    assert client.post("/agent/live", data={"authorize": "1", "confirm": "AUTHORIZE LIVE", "csrf": csrf}).status_code == 403
    assert client.post("/settings/scoring", data={"csrf": csrf}).status_code == 403
    assert client.get("/audit").status_code == 403
    _ = admin


def test_manual_stage_cannot_fake_workflow_states(db, client):
    admin = make_user(db)
    run_source(db, "fixture")
    db.commit()
    csrf = login(client, admin.email)
    pid = client.get("/api/v1/prospects").json()["items"][0]["id"]
    for stage in ("contacted", "meeting_booked", "replied", "outreach_approved"):
        r = client.post(f"/api/v1/prospects/{pid}/stage", json={"stage": stage}, headers={"x-csrf-token": csrf})
        assert r.status_code == 409, stage


def test_api_key_auth(db, client):
    admin = make_user(db)
    csrf = login(client, admin.email)
    r = client.post("/settings/api-key", data={"csrf": csrf})
    key = r.text.split("New API key (shown once): ")[1].split("<")[0].strip()
    from fastapi.testclient import TestClient

    from dealerflow.web.app import create_app

    anon = TestClient(create_app())
    assert anon.get("/api/v1/prospects", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = anon.post("/api/v1/prospects", headers={"Authorization": f"Bearer {key}"},
                  json={"name": "API Motors (SIMULATED)", "city": "Sugar Land", "phone": "281-555-0199"})
    assert r.status_code == 201 and r.json()["created"]
    r = anon.post("/api/v1/prospects", headers={"Authorization": f"Bearer {key}"},
                  json={"name": "API Motors LLC", "phone": "(281) 555-0199"})
    assert r.json()["created"] is False and r.json()["matched_by"] == "phone"


def test_bulk_requires_confirmation_and_blocks_workflow_stages(db, client):
    admin = make_user(db)
    run_source(db, "fixture")
    db.commit()
    csrf = login(client, admin.email)
    ids = [p["id"] for p in client.get("/api/v1/prospects").json()["items"][:3]]
    r = client.post("/prospects/bulk", data={"ids": ids, "action": "stage", "value": "researching", "csrf": csrf})
    assert "Type the number" in r.text
    r = client.post("/prospects/bulk", data={"ids": ids, "action": "stage", "value": "contacted", "confirm": "3", "csrf": csrf})
    assert "set by its workflow" in r.text
    r = client.post("/prospects/bulk", data={"ids": ids, "action": "stage", "value": "researching", "confirm": "3", "csrf": csrf})
    assert "3 updated" in r.text


def test_zoom_webhook_signature(db, client, monkeypatch):
    import hashlib
    import hmac
    import json
    import time

    from dealerflow.config import get_settings

    monkeypatch.setenv("DF_ZOOM_WEBHOOK_SECRET_TOKEN", "whsec")
    get_settings.cache_clear()
    body = json.dumps({"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}})
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"whsec", f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    r = client.post("/webhooks/zoom", content=body, headers={"x-zm-request-timestamp": ts, "x-zm-signature": sig})
    assert r.status_code == 200 and r.json()["encryptedToken"] == hmac.new(b"whsec", b"abc", hashlib.sha256).hexdigest()
    r = client.post("/webhooks/zoom", content=body, headers={"x-zm-request-timestamp": ts, "x-zm-signature": "v0=bad"})
    assert r.status_code == 401


def test_untrusted_urls_are_not_rendered_as_links(db, client):
    from dealerflow.models import Evidence, Prospect

    admin = make_user(db)
    p = Prospect(name="XSS Motors", normalized_name="xss motors", stage="new")
    db.add(p)
    db.flush()
    db.add(Evidence(prospect_id=p.id, field="name", value="x", source_type="csv:evil",
                    source_url="javascript:alert(document.cookie)"))
    db.commit()
    login(client, admin.email)
    html = client.get(f"/prospects/{p.id}").text
    assert "javascript:alert" not in html


def test_safe_next_blocks_open_redirects():
    from dealerflow.web.deps import safe_next

    assert safe_next("/prospects?x=1") == "/prospects?x=1"
    assert safe_next("http://127.0.0.1:8000/tasks") == "/"
    for bad in ("//evil.com", "/\\\\evil.com", "https://evil.com/x", "javascript:alert(1)"):
        assert safe_next(bad) == "/", bad


def test_back_redirect_uses_same_host_referer_only(db, client):
    admin = make_user(db)
    csrf = login(client, admin.email)
    from dealerflow.models import Task
    from dealerflow.db import utcnow
    t = Task(title="t", due_at=utcnow())
    db.add(t)
    db.commit()
    r = client.post(f"/tasks/{t.id}/done", data={"csrf": csrf}, headers={"referer": "http://testserver/tasks?show=all"},
                    follow_redirects=False)
    assert r.headers["location"] == "/tasks?show=all"
    t2 = Task(title="t2", due_at=utcnow())
    db.add(t2)
    db.commit()
    r = client.post(f"/tasks/{t2.id}/done", data={"csrf": csrf}, headers={"referer": "https://evil.example/x"},
                    follow_redirects=False)
    assert r.headers["location"] == "/tasks"


def test_detail_pages_render_through_the_ui_workflow(db, client):
    """Drive the main workflow through HTTP and render every detail page along the way."""
    from dealerflow.models import AgentRun, InboundMessage, Meeting, OutreachMessage, Template

    admin = make_user(db)
    run_source(db, "fixture")
    db.commit()
    csrf = login(client, admin.email)
    p = client.get("/api/v1/prospects", params={"q": "Brazos"}).json()["items"][0]
    tpl = db.scalar(select(Template).where(Template.name == "intro", Template.language == "es"))
    r = client.post(f"/prospects/{p['id']}/draft", data={"template_id": tpl.id, "csrf": csrf})
    assert r.status_code == 200 and "Seguimiento de clientes" in r.text and "cancele su suscripción" in r.text
    mid = db.scalar(select(OutreachMessage.id))
    r = client.post(f"/outreach/{mid}/approve", data={"csrf": csrf, "send_now": "1"})
    assert "SIMULATED" in r.text
    r = client.post("/inbox/simulate", data={"prospect_id": p["id"], "csrf": csrf,
                                             "body": "Me interesa. ¿Podemos agendar una llamada por Zoom?"})
    assert r.status_code == 200
    iid = db.scalar(select(InboundMessage.id))
    assert "meeting_request" in client.get(f"/inbox/{iid}").text
    meeting_id = db.scalar(select(Meeting.id))
    client.post(f"/meetings/{meeting_id}/availability", data={"csrf": csrf, "days": "7"})
    db.expire_all()
    slot = db.get(Meeting, meeting_id).proposed_slots[0]["start"]
    client.post(f"/meetings/{meeting_id}/choose", data={"csrf": csrf, "start": slot})
    r = client.post(f"/meetings/{meeting_id}/approve", data={"csrf": csrf})
    assert "Booked" in r.text and "simulated.invalid" in r.text
    client.post("/agent/tick", data={"csrf": csrf})
    for path in (f"/prospects/{p['id']}", f"/outreach/{mid}", f"/inbox/{iid}", f"/meetings/{meeting_id}", "/",
                 "/agent", "/reports/weekly?simulated=1", "/audit", "/integrations"):
        assert client.get(path).status_code == 200, path
    run_id = db.scalar(select(AgentRun.id))
    assert client.get(f"/agent/runs/{run_id}").status_code == 200
