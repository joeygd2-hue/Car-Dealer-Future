"""Role matrix: dealer staff vs dealer admin vs platform admin on state-changing endpoints."""

import pytest
from sqlalchemy import select

from app import db as dbmod
from app.models import Notification, SendAttempt
from tests.conftest import login, run_worker

ADMIN_ONLY_PORTAL_POSTS = [
    ("/settings/business", {"phone": "1"}),
    ("/settings/faqs", {"question": "q", "answer": "a"}),
    ("/settings/templates", {"intent": "price", "body": "x"}),
    ("/settings/automation/limits", {"max_conv": 1, "max_dealer": 1, "min_interval": 5, "stale_hours": 24, "threshold": 0.9}),
    ("/settings/automation/evaluate", {}),
    ("/settings/automation/mode", {"mode": "A_DRAFT_ONLY"}),
    ("/settings/notifications", {}),
]


@pytest.mark.parametrize("path,data", ADMIN_ONLY_PORTAL_POSTS)
def test_staff_cannot_use_dealer_admin_actions(client, world, path, data):
    csrf = login(client, "staff@a.example")
    r = client.post(f"/portal/{world['a']['d'].id}{path}", data={**data, "csrf_token": csrf})
    assert r.status_code == 403, path


@pytest.mark.parametrize("path,data", ADMIN_ONLY_PORTAL_POSTS)
def test_other_dealer_admin_cannot_use_them_either(client, world, path, data):
    csrf = login(client, "admin@b.example")
    r = client.post(f"/portal/{world['a']['d'].id}{path}", data={**data, "csrf_token": csrf})
    assert r.status_code == 404, path


@pytest.mark.parametrize("path", ["/admin/global-pause", "/admin/pilot/emergency-stop", "/admin/pilot/return-to-sandbox",
                                  "/admin/live-authorizations", "/admin/evaluations/run", "/admin/outreach/control/start",
                                  "/admin/users/1/reset-link", "/admin/users/1/deactivate", "/admin/dealerships"])
def test_platform_only_actions_forbidden_to_dealer_admin(client, world, path):
    csrf = login(client, "admin@a.example")
    r = client.post(path, data={"csrf_token": csrf, "on": 1, "scope": "x", "statement": "x" * 30, "name": "x"})
    assert r.status_code == 403, path


def test_uncertain_notification_reconciliation(client, db, world):
    from app.models import IntegrationConnection
    from app.services import notifications

    db.add(IntegrationConnection(dealership_id=None, provider="NOTIFY_EMAIL", external_account_id="smtp",
                                 config={"sandbox_failure_mode": "uncertain"}))
    db.commit()
    notifications.notify(db, world["a"]["d"].id, kind="ESCALATION", subject="unc", body="b", dedupe_key="unc")
    db.commit()
    run_worker()
    n = db.scalar(select(Notification).where(Notification.subject == "unc", Notification.channel == "EMAIL"))
    assert n.status == "UNCERTAIN"
    csrf = login(client, "ops@platform.example")
    assert "Notification #" in client.get("/admin/pilot").text
    client.post(f"/admin/pilot/reconcile/notification/{n.id}", data={"delivered": 1, "csrf_token": csrf})
    with dbmod.SessionLocal() as s:
        assert s.get(Notification, n.id).status == "SENT"
        assert s.scalar(select(SendAttempt).where(SendAttempt.kind == "NOTIFICATION_EMAIL", SendAttempt.ref_id == n.id)).status == "UNCERTAIN"
