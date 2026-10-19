"""Staff notifications: in-app (always) + email (when configured/authorized), with real delivery status.

Status meanings shown in the UI:
  DELIVERED  — stored in the in-app notification center
  SENT       — accepted by the live email provider
  SIMULATED  — sandbox mode: NOT actually emailed
  FAILED     — provider error after retries
  SKIPPED    — no deliverable address / preference disabled
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import utcnow
from ...models import (
    ResponseAutomationSettings,
    Conversation,
    Client,
    Escalation,
    Notification,
    NotificationPreference,
    ResponseDraft,
    User,
)
from ...models import Appointment
from . import channels, runtime


def _members(db: Session, client_id: int) -> list[User]:
    """The dealership's own users (never agency staff and never another dealership's users)."""
    return list(db.scalars(select(User).where(User.client_id == client_id)))


def get_prefs(db: Session, dealership_id: int) -> NotificationPreference:
    prefs = db.scalar(select(NotificationPreference).where(NotificationPreference.client_id == dealership_id))
    if prefs is None:
        prefs = NotificationPreference(client_id=dealership_id)
        db.add(prefs)
        db.flush()
    return prefs


def recipients(db: Session, dealership_id: int, *, assigned_user_id: int | None, escalation_reasons: list[str] | None = None,
               kind: str = "ESCALATION") -> tuple[list[User], list[str]]:
    """Users (in-app + email) and extra email-only addresses that should be notified."""
    prefs = get_prefs(db, dealership_id)
    members = _members(db, dealership_id)
    member_ids = {m.id for m in members}
    users: dict[int, User] = {}
    if assigned_user_id and prefs.notify_assigned_user:
        u = db.get(User, assigned_user_id)
        if u and u.active and u.id in member_ids:  # never notify outside the tenant
            users[u.id] = u
    admins_needed = (not users) or kind != "ESCALATION" or bool(
        set(escalation_reasons or []) & set(prefs.escalate_to_admins_reasons or []))
    for m in members:
        # Dealership admins receive every escalation (Phase 3's DEALER_ADMIN + receives_escalations).
        if m.active and m.client_role == "dealer_admin" and (admins_needed or kind == "ESCALATION"):
            users[m.id] = m
    extra: list[str] = []
    if kind == "ESCALATION":
        settings = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == dealership_id))
        extra = [e for e in (settings.escalation_emails if settings else []) if e]
    return list(users.values()), extra


def notify(db: Session, dealership_id: int, *, kind: str, subject: str, body: str, link: str | None = None,
           assigned_user_id: int | None = None, escalation_reasons: list[str] | None = None,
           dedupe_key: str | None = None, immediate: bool = True) -> list[Notification]:
    prefs = get_prefs(db, dealership_id)
    users, extra = recipients(db, dealership_id, assigned_user_id=assigned_user_id,
                              escalation_reasons=escalation_reasons, kind=kind)
    created: list[Notification] = []
    base = get_settings().base_url.rstrip("/")
    full_link = f"{base}{link}" if link and link.startswith("/") else link
    for u in users:
        key = f"{dedupe_key}:inapp:{u.id}" if dedupe_key else None
        if key and db.scalar(select(Notification.id).where(Notification.idempotency_key == key)):
            continue
        n = Notification(client_id=dealership_id, user_id=u.id, channel="IN_APP", kind=kind, subject=subject,
                         body=body, link=link, status="DELIVERED", sent_at=utcnow(), idempotency_key=key)
        db.add(n)
        created.append(n)
    want_email = prefs.email_enabled and (immediate and (prefs.immediate_alerts or kind == "ESCALATION") or kind == "DIGEST")
    addresses = [(u.id, u.email) for u in users] + [(None, e) for e in extra]
    for user_id, address in addresses:
        key = f"{dedupe_key}:email:{address}" if dedupe_key else None
        if key and db.scalar(select(Notification.id).where(Notification.idempotency_key == key)):
            continue
        n = Notification(client_id=dealership_id, user_id=user_id, recipient_address=address, channel="EMAIL",
                         kind=kind, subject=subject, body=body + (f"\n\nOpen: {full_link}" if full_link else ""),
                         link=link, status="PENDING" if want_email else "SKIPPED",
                         status_detail=None if want_email else "email alerts disabled by preference",
                         idempotency_key=key)
        db.add(n)
        db.flush()
        if want_email:
            runtime.enqueue(db, "notification.send_email", {"notification_id": n.id},
                         key=f"notify-email:{n.id}", max_attempts=4)
        created.append(n)
    db.flush()
    return created


def _mark_failed(db: Session, payload: dict, error: str) -> None:
    n = db.get(Notification, payload.get("notification_id"))
    if n and n.status not in ("SENT", "SIMULATED"):
        n.status = "FAILED"
        n.status_detail = (error or "")[:500]
        runtime.record(db, "notification.failed", actor_type="WORKER", client_id=n.client_id,
                     entity_type="notification", entity_id=n.id, outcome="FAILED", external=True)


@runtime.handler("notification.send_email", on_dead=_mark_failed)
def send_email_job(db: Session, payload: dict) -> dict:
    from sqlalchemy import update

    from . import send_gate

    nid = runtime.payload_get(payload, "notification_id")
    n = db.get(Notification, nid)
    if n is None:
        return {"skipped": True}
    if n.status == "SENDING":
        verdict, wait, attempt = send_gate.check_sending_row(db, "NOTIFICATION_EMAIL", n.id)
        if verdict != "crashed":
            raise runtime.RetryLater(wait, "another worker is sending (or just sent) this notification")
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
    sender, live = channels.notification_email_sender(db)
    gate = send_gate.open_attempt(db, kind="NOTIFICATION_EMAIL", ref_id=n.id, channel="EMAIL", recipient=n.recipient_address,
                                  live=live, client_id=n.client_id)
    if not gate.allowed:
        n.status, n.status_detail = "BLOCKED", "; ".join(gate.reasons)
        return {"blocked": gate.reasons}
    n.attempts += 1
    db.commit()
    settings = get_settings()
    app_name = settings.business_name or "dealerflow"
    result = sender.send(to=n.recipient_address, subject=f"[{app_name}] {n.subject}", body=n.body,
                         from_addr=settings.sender_email or "notifications@sandbox.invalid", from_name=app_name)
    send_gate.close_attempt(db, gate.attempt, result)
    if result.ok:
        n.status = result.status  # SENT or SIMULATED
        n.provider_message_id = result.provider_message_id
        n.sent_at = utcnow()
        n.status_detail = None if live else "sandbox: email not actually sent (live email not authorized/configured)"
        return {"status": n.status}
    n.status_detail = result.error
    if result.uncertain:
        n.status = "UNCERTAIN"
        return {"uncertain": result.error}
    if result.retryable:
        n.status = "PENDING"
        db.commit()
        raise runtime.TransientSendError(result.error or "transient email failure")
    n.status = "FAILED"
    runtime.record(db, "notification.failed", actor_type="WORKER", client_id=n.client_id,
                 entity_type="notification", entity_id=n.id, outcome="FAILED", external=True)
    return {"failed": result.error}


# ------------------------------------------------------------------ daily digest


def build_digest(db: Session, dealership: Client) -> tuple[str, str]:
    since = utcnow() - timedelta(days=1)
    new_convs = db.scalar(select(func.count(Conversation.id)).where(
        Conversation.client_id == dealership.id, Conversation.created_at >= since)) or 0
    open_esc = list(db.scalars(select(Escalation).where(
        Escalation.client_id == dealership.id, Escalation.status != "RESOLVED").limit(20)))
    pending = db.scalar(select(func.count(ResponseDraft.id)).where(
        ResponseDraft.client_id == dealership.id, ResponseDraft.status == "PENDING_REVIEW")) or 0
    appts = list(db.scalars(select(Appointment).where(
        Appointment.client_id == dealership.id, Appointment.status.in_(["REQUESTED", "PENDING_CONFIRMATION"]))))
    lines = [f"Daily summary for {dealership.name}", "",
             f"New conversations (24h): {new_convs}", f"Drafts awaiting review: {pending}",
             f"Open escalations: {len(open_esc)}", f"Appointments awaiting confirmation: {len(appts)}", ""]
    for e in open_esc:
        lines.append(f"- Escalation #{e.id} ({', '.join(e.reasons)}): {e.recommended_action}")
    return f"Daily summary — {dealership.name}", "\n".join(lines)


@runtime.handler("notification.daily_digests")
def daily_digests_job(db: Session, payload: dict) -> dict:
    sent = 0
    for d in db.scalars(select(Client).where(Client.status.in_(("onboarding", "active")))):
        prefs = get_prefs(db, d.id)
        if not prefs.daily_digest:
            continue
        local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(d.timezone))
        if local.hour != prefs.digest_hour_local:
            continue
        subject, body = build_digest(db, d)
        notify(db, d.id, kind="DIGEST", subject=subject, body=body, link=f"/dealers/{d.id}/inbox",
               dedupe_key=f"digest:{d.id}:{local.date().isoformat()}")
        sent += 1
    return {"digests": sent}


def notify_operator(db: Session, *, kind: str, subject: str, body: str, link: str | None = None,
                    dedupe_key: str | None = None) -> list[Notification]:
    """Notify agency administrators (staff users with the admin role) — send failures and pilot alerts."""
    created = []
    base = get_settings().base_url.rstrip("/")
    for u in db.scalars(select(User).where(User.role == "admin", User.client_id.is_(None), User.active.is_(True))):
        for channel in ("IN_APP", "EMAIL"):
            key = f"{dedupe_key}:{channel}:{u.id}" if dedupe_key else None
            if key and db.scalar(select(Notification.id).where(Notification.idempotency_key == key)):
                continue
            n = Notification(user_id=u.id, recipient_address=u.email if channel == "EMAIL" else None, channel=channel,
                             kind=kind, subject=subject,
                             body=body + (f"\n\nOpen: {base}{link}" if link and channel == "EMAIL" else ""), link=link,
                             status="DELIVERED" if channel == "IN_APP" else "PENDING",
                             sent_at=utcnow() if channel == "IN_APP" else None, idempotency_key=key)
            db.add(n)
            db.flush()
            if channel == "EMAIL":
                runtime.enqueue(db, "notification.send_email", {"notification_id": n.id},
                             key=f"notify-email:{n.id}", max_attempts=4)
            created.append(n)
    return created
