"""Staff notifications for customer conversations: in-app (always) + email (when configured/authorized).

Status meanings shown in the UI:
  DELIVERED  stored in the in-app notification center
  SENT       accepted by the real email provider (client email enabled and authorized for this dealership)
  SIMULATED  NOT actually emailed (mock provider)
  SKIPPED    not emailed: no address, preference off, or client email not enabled/authorized for this dealership
  FAILED     provider error after retries
Email goes through the send gate (emergency stop + ledger) and Phase 4's gated ``client_email.send``.
Operator-level alerts use Phase 4's ``alerts.raise_alert``.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import utcnow
from ...jobs import DeferJob, enqueue, handler, on_failure
from ...models import (
    Appointment,
    Client,
    Conversation,
    Escalation,
    Notification,
    NotificationPreference,
    ResponseAutomationSettings,
    ResponseDraft,
    User,
)
from .. import alerts, client_email
from . import send_gate
from .channels import SendResult
from .common import local_now, record

EMAIL_KIND = "NOTIFICATION_EMAIL"


def get_prefs(db: Session, client_id: int) -> NotificationPreference:
    prefs = db.scalar(select(NotificationPreference).where(NotificationPreference.client_id == client_id))
    if prefs is None:
        prefs = NotificationPreference(client_id=client_id)
        db.add(prefs)
        db.flush()
    return prefs


def _eligible(db: Session, client_id: int, user_id: int | None) -> User | None:
    """A user who may receive this dealership's notifications: its own active users, or active agency staff."""
    if not user_id:
        return None
    u = db.get(User, user_id)
    if u is None or not u.active:
        return None
    return u if u.client_id == client_id or (u.client_id is None and u.role in ("rep", "manager", "admin")) else None


def recipients(db: Session, client_id: int, *, assigned_user_id: int | None, escalation_reasons: list[str] | None = None,
               kind: str = "ESCALATION") -> tuple[list[User], list[str]]:
    """Users (in-app + email) and extra email-only addresses that should be notified. Never outside the tenant."""
    prefs = get_prefs(db, client_id)
    users: dict[int, User] = {}
    assigned = _eligible(db, client_id, assigned_user_id) if prefs.notify_assigned_user else None
    if assigned:
        users[assigned.id] = assigned
    admins_needed = (not users) or kind != "ESCALATION" or bool(
        set(escalation_reasons or []) & set(prefs.escalate_to_admins_reasons or []))
    for u in db.scalars(select(User).where(User.client_id == client_id, User.active.is_(True))):
        if (admins_needed and u.client_role == "dealer_admin") or (kind == "ESCALATION" and u.id in (prefs.escalation_user_ids or [])):
            users[u.id] = u
    extra: list[str] = []
    if kind == "ESCALATION":
        settings = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == client_id))
        extra = [e for e in (settings.escalation_emails if settings else []) if e]
    return list(users.values()), extra


def notify(db: Session, client_id: int, *, kind: str, subject: str, body: str, link: str | None = None,
           assigned_user_id: int | None = None, escalation_reasons: list[str] | None = None,
           dedupe_key: str | None = None, immediate: bool = True) -> list[Notification]:
    prefs = get_prefs(db, client_id)
    users, extra = recipients(db, client_id, assigned_user_id=assigned_user_id,
                              escalation_reasons=escalation_reasons, kind=kind)
    created: list[Notification] = []
    base = get_settings().base_url.rstrip("/")
    full_link = f"{base}{link}" if link and link.startswith("/") else link
    for u in users:
        key = f"{dedupe_key}:inapp:{u.id}" if dedupe_key else None
        if key and db.scalar(select(Notification.id).where(Notification.idempotency_key == key)):
            continue
        n = Notification(client_id=client_id, user_id=u.id, channel="IN_APP", kind=kind, subject=subject[:300],
                         body=body, link=link, status="DELIVERED", sent_at=utcnow(), idempotency_key=key)
        db.add(n)
        created.append(n)
    want_email = prefs.email_enabled and (immediate and (prefs.immediate_alerts or kind == "ESCALATION") or kind == "DIGEST")
    addresses = [(u.id, u.email) for u in users] + [(None, e) for e in extra]
    for user_id, address in addresses:
        key = f"{dedupe_key}:email:{address}" if dedupe_key else None
        if key and db.scalar(select(Notification.id).where(Notification.idempotency_key == key)):
            continue
        n = Notification(client_id=client_id, user_id=user_id, recipient_address=address, channel="EMAIL", kind=kind,
                         subject=subject[:300], body=body + (f"\n\nOpen: {full_link}" if full_link else ""), link=link,
                         status="PENDING" if want_email else "SKIPPED",
                         status_detail=None if want_email else "email alerts disabled by preference", idempotency_key=key)
        db.add(n)
        db.flush()
        if want_email:
            enqueue(db, "conversations.notification_email", f"conv-notify-email:{n.id}", {"notification_id": n.id},
                    max_attempts=4)
        created.append(n)
    db.flush()
    return created


def notify_operator(db: Session, *, subject: str, body: str, dedupe_key: str | None = None) -> None:
    """Agency-level alert (Phase 4 alerts page)."""
    alerts.raise_alert(db, "customer_messaging", f"{subject}\n{body}"[:2000], severity="warning", dedupe_key=dedupe_key)


@on_failure("conversations.notification_email")
def _mark_failed(db: Session, payload: dict, error: str) -> None:
    n = db.get(Notification, payload.get("notification_id"))
    if n and n.status not in ("SENT", "SIMULATED"):
        n.status, n.status_detail = "FAILED", (error or "")[:500]
        record(db, "notification.failed", client_id=n.client_id, entity_type="notification", entity_id=n.id, outcome="FAILED")


@handler("conversations.notification_email")
def send_email_job(db: Session, payload: dict) -> dict:
    nid = int(payload["notification_id"])
    n = db.get(Notification, nid)
    if n is None:
        return {"skipped": True}
    if n.status == "SENDING":
        verdict, wait, attempt = send_gate.check_sending_row(db, EMAIL_KIND, n.id)
        if verdict != "crashed":
            raise DeferJob(utcnow() + send_gate.defer_seconds(wait), "another worker is sending (or just sent) this notification")
        if not send_gate.take_over_crashed(db, attempt, Notification, "status", n.id):
            return {"skipped": "owner finished"}
        db.refresh(n)
        n.status_detail = "worker stopped mid-send; not re-sent automatically"
        return {"uncertain": True}
    if not n.recipient_address:
        n.status, n.status_detail = "SKIPPED", "no email address"
        return {"skipped": True}
    if db.execute(update(Notification).where(Notification.id == nid, Notification.status == "PENDING")
                  .values(status="SENDING").execution_options(synchronize_session=False)).rowcount != 1:
        return {"skipped": n.status}
    db.refresh(n)
    client = db.get(Client, n.client_id)
    allowed, _why = client_email.delivery_allowed(db, client)
    gate = send_gate.open_attempt(db, kind=EMAIL_KIND, ref_id=n.id, channel="EMAIL", recipient=n.recipient_address,
                                  live=allowed, client_id=n.client_id)
    if not gate.allowed:
        n.status, n.status_detail = "BLOCKED", "; ".join(gate.reasons)
        return {"blocked": gate.reasons}
    n.attempts += 1
    db.commit()  # claim + ledger IN_FLIGHT are durable before the provider sees anything
    outcome = client_email.send(db, client, to=n.recipient_address, subject=n.subject, body=n.body,
                                key=f"conv-notify:{n.id}")
    status = {"sent": "SENT", "simulated": "SIMULATED"}.get(outcome, "SKIPPED")
    if status == "SKIPPED":  # client email not enabled/authorized for this dealership: nothing left the system
        gate.attempt.status, gate.attempt.reasons = "BLOCKED", [outcome]
    else:
        send_gate.close_attempt(db, gate.attempt, SendResult(status))
    n.status = status
    n.sent_at = utcnow() if status in ("SENT", "SIMULATED") else None
    n.status_detail = {"SENT": None, "SIMULATED": "mock email provider: NOT actually sent"}.get(status, outcome)
    return {"status": n.status}


# --- daily digest ---------------------------------------------------------------------------------------------


def build_digest(db: Session, client: Client) -> tuple[str, str]:
    since = utcnow() - timedelta(days=1)
    new_convs = db.scalar(select(func.count(Conversation.id)).where(
        Conversation.client_id == client.id, Conversation.created_at >= since)) or 0
    open_esc = list(db.scalars(select(Escalation).where(
        Escalation.client_id == client.id, Escalation.status != "RESOLVED").limit(20)))
    pending = db.scalar(select(func.count(ResponseDraft.id)).where(
        ResponseDraft.client_id == client.id, ResponseDraft.status == "PENDING_REVIEW")) or 0
    appts = list(db.scalars(select(Appointment).where(
        Appointment.client_id == client.id, Appointment.status.in_(["REQUESTED", "PENDING_CONFIRMATION"]))))
    lines = [f"Daily summary for {client.name}", "",
             f"New conversations (24h): {new_convs}", f"Drafts awaiting review: {pending}",
             f"Open escalations: {len(open_esc)}", f"Appointments awaiting confirmation: {len(appts)}", ""]
    for e in open_esc:
        lines.append(f"- Escalation #{e.id} ({', '.join(e.reasons)}): {e.recommended_action}")
    return f"Daily summary — {client.name}", "\n".join(lines)


@handler("conversations.daily_digests")
def daily_digests_job(db: Session, payload: dict) -> dict:
    sent = 0
    for c in db.scalars(select(Client).where(Client.status != "cancelled")):
        prefs = get_prefs(db, c.id)
        if not prefs.daily_digest:
            continue
        local = local_now(c.timezone)
        if local.hour != prefs.digest_hour_local:
            continue
        subject, body = build_digest(db, c)
        notify(db, c.id, kind="DIGEST", subject=subject, body=body, link=f"/dealers/{c.id}/inbox",
               dedupe_key=f"digest:{c.id}:{local.date().isoformat()}")
        sent += 1
    return {"digests": sent}
