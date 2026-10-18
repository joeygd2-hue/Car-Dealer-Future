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

from app.config import get_settings
from app.db import utcnow
from app.integrations import registry
from app.models import (
    AutomationSettings,
    Conversation,
    Dealership,
    Escalation,
    Membership,
    Notification,
    NotificationPreference,
    ResponseDraft,
    User,
)
from app.models.appointments import Appointment
from app.services import audit, jobs


def get_prefs(db: Session, dealership_id: int) -> NotificationPreference:
    prefs = db.scalar(select(NotificationPreference).where(NotificationPreference.dealership_id == dealership_id))
    if prefs is None:
        prefs = NotificationPreference(dealership_id=dealership_id)
        db.add(prefs)
        db.flush()
    return prefs


def recipients(db: Session, dealership_id: int, *, assigned_user_id: int | None, escalation_reasons: list[str] | None = None,
               kind: str = "ESCALATION") -> tuple[list[User], list[str]]:
    """Users (in-app + email) and extra email-only addresses that should be notified."""
    prefs = get_prefs(db, dealership_id)
    members = list(db.scalars(select(Membership).where(Membership.dealership_id == dealership_id)))
    users: dict[int, User] = {}
    if assigned_user_id and prefs.notify_assigned_user:
        u = db.get(User, assigned_user_id)
        if u and u.is_active and any(m.user_id == u.id for m in members):  # never notify outside the tenant
            users[u.id] = u
    admins_needed = (not users) or kind != "ESCALATION" or bool(
        set(escalation_reasons or []) & set(prefs.escalate_to_admins_reasons or []))
    for m in members:
        if (admins_needed and m.role == "DEALER_ADMIN") or (kind == "ESCALATION" and m.receives_escalations):
            if m.user.is_active:
                users[m.user_id] = m.user
    extra: list[str] = []
    if kind == "ESCALATION":
        settings = db.scalar(select(AutomationSettings).where(AutomationSettings.dealership_id == dealership_id))
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
        n = Notification(dealership_id=dealership_id, user_id=u.id, channel="IN_APP", kind=kind, subject=subject,
                         body=body, link=link, status="DELIVERED", sent_at=utcnow(), idempotency_key=key)
        db.add(n)
        created.append(n)
    want_email = prefs.email_enabled and (immediate and (prefs.immediate_alerts or kind == "ESCALATION") or kind == "DIGEST")
    addresses = [(u.id, u.email) for u in users] + [(None, e) for e in extra]
    for user_id, address in addresses:
        key = f"{dedupe_key}:email:{address}" if dedupe_key else None
        if key and db.scalar(select(Notification.id).where(Notification.idempotency_key == key)):
            continue
        n = Notification(dealership_id=dealership_id, user_id=user_id, recipient_address=address, channel="EMAIL",
                         kind=kind, subject=subject, body=body + (f"\n\nOpen: {full_link}" if full_link else ""),
                         link=link, status="PENDING" if want_email else "SKIPPED",
                         status_detail=None if want_email else "email alerts disabled by preference",
                         idempotency_key=key)
        db.add(n)
        db.flush()
        if want_email:
            jobs.enqueue(db, "notification.send_email", {"notification_id": n.id},
                         idempotency_key=f"notify-email:{n.id}", max_attempts=4)
        created.append(n)
    db.flush()
    return created


def _mark_failed(db: Session, payload: dict, job) -> None:
    n = db.get(Notification, payload.get("notification_id"))
    if n and n.status not in ("SENT", "SIMULATED"):
        n.status = "FAILED"
        n.status_detail = (job.last_error or "")[:500]
        audit.record(db, "notification.failed", actor_type="WORKER", dealership_id=n.dealership_id,
                     entity_type="notification", entity_id=n.id, outcome="FAILED", external=True)


@jobs.handler("notification.send_email", on_dead=_mark_failed)
def send_email_job(db: Session, payload: dict, job) -> dict:
    n = db.get(Notification, jobs.payload_get(payload, "notification_id"))
    if n is None or n.status in ("SENT", "SIMULATED", "SKIPPED"):
        return {"skipped": True}
    if not n.recipient_address:
        n.status, n.status_detail = "SKIPPED", "no email address"
        return {"skipped": True}
    sender, live = registry.notification_email_sender(db)
    settings = get_settings()
    n.attempts += 1
    result = sender.send(to=n.recipient_address, subject=f"[{settings.app_name}] {n.subject}", body=n.body,
                         from_addr=settings.smtp_from or "notifications@sandbox.invalid", from_name=settings.app_name)
    if result.ok:
        n.status = result.status  # SENT or SIMULATED
        n.provider_message_id = result.provider_message_id
        n.sent_at = utcnow()
        n.status_detail = None if live else "sandbox: email not actually sent (live email not authorized/configured)"
        audit.record(db, "notification.email_sent" if live else "notification.email_simulated", actor_type="WORKER",
                     dealership_id=n.dealership_id, entity_type="notification", entity_id=n.id,
                     outcome="OK" if live else "SIMULATED", external=live)
        return {"status": n.status}
    n.status_detail = result.error
    if result.retryable:
        raise RuntimeError(result.error)
    raise jobs.PermanentJobError(result.error or "email rejected")


# ------------------------------------------------------------------ daily digest


def build_digest(db: Session, dealership: Dealership) -> tuple[str, str]:
    since = utcnow() - timedelta(days=1)
    new_convs = db.scalar(select(func.count(Conversation.id)).where(
        Conversation.dealership_id == dealership.id, Conversation.created_at >= since)) or 0
    open_esc = list(db.scalars(select(Escalation).where(
        Escalation.dealership_id == dealership.id, Escalation.status != "RESOLVED").limit(20)))
    pending = db.scalar(select(func.count(ResponseDraft.id)).where(
        ResponseDraft.dealership_id == dealership.id, ResponseDraft.status == "PENDING_REVIEW")) or 0
    appts = list(db.scalars(select(Appointment).where(
        Appointment.dealership_id == dealership.id, Appointment.status.in_(["REQUESTED", "PENDING_CONFIRMATION"]))))
    lines = [f"Daily summary for {dealership.name}", "",
             f"New conversations (24h): {new_convs}", f"Drafts awaiting review: {pending}",
             f"Open escalations: {len(open_esc)}", f"Appointments awaiting confirmation: {len(appts)}", ""]
    for e in open_esc:
        lines.append(f"- Escalation #{e.id} ({', '.join(e.reasons)}): {e.recommended_action}")
    return f"Daily summary — {dealership.name}", "\n".join(lines)


@jobs.handler("notification.daily_digests")
def daily_digests_job(db: Session, payload: dict, job) -> dict:
    sent = 0
    for d in db.scalars(select(Dealership).where(Dealership.status != "SUSPENDED")):
        prefs = get_prefs(db, d.id)
        if not prefs.daily_digest:
            continue
        local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(d.timezone))
        if local.hour != prefs.digest_hour_local:
            continue
        subject, body = build_digest(db, d)
        notify(db, d.id, kind="DIGEST", subject=subject, body=body, link=f"/portal/{d.id}",
               dedupe_key=f"digest:{d.id}:{local.date().isoformat()}")
        sent += 1
    return {"digests": sent}


def notify_operator(db: Session, *, kind: str, subject: str, body: str, link: str | None = None,
                    dedupe_key: str | None = None) -> list[Notification]:
    """Notify platform administrators (the operator) — used by the outreach agent and admin alerts."""
    created = []
    base = get_settings().base_url.rstrip("/")
    for u in db.scalars(select(User).where(User.is_platform_admin.is_(True), User.is_active.is_(True))):
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
                jobs.enqueue(db, "notification.send_email", {"notification_id": n.id},
                             idempotency_key=f"notify-email:{n.id}", max_attempts=4)
            created.append(n)
    return created
