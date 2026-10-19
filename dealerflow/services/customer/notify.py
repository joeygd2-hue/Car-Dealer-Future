"""Notifications to dealership users about customer conversations: in-app (always) + email, with real status.

Status meanings shown in the UI:
  DELIVERED  — stored in the in-app notification list
  SENT       — accepted by the real email provider
  SIMULATED  — the email provider is the SIMULATED mock: NOT actually emailed
  NOT_SENT   — a real provider is configured but this client's email delivery is not authorized
  FAILED / UNCERTAIN / BLOCKED / SKIPPED — see ``status_detail``

Email goes through Phase 4's ``client_email`` gate (``DF_CLIENT_EMAIL_SENDING_ENABLED`` + the client's
``report_email`` connection) and through the send ledger, so a crash mid-send is never silently repeated.
Recipients are only users of that dealership (``users.client_id``) — never another dealership's users — plus
agency staff who were explicitly assigned to the conversation and the dealership's escalation email list.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ... import audit
from ...config import get_settings
from ...db import utcnow
from ...integrations.customer_channels import SendResult
from ...integrations.errors import (
    CredentialError,
    ExternalActionsHalted,
    IntegrationDisabled,
    IntegrationError,
    PermanentError,
    RateLimitedError,
)
from ...jobs import DeferJob, enqueue, handler, on_failure
from ...models import User
from ...models_customer import (
    CustomerConversation,
    DealerNotification,
    DealerNotificationPreference,
    Escalation,
    ResponseAutomationSettings,
    ResponseDraft,
    TestDriveAppointment,
)
from ...models_dealer import Client

KIND = "DEALER_NOTIFICATION_EMAIL"


def get_prefs(db: Session, client_id: int) -> DealerNotificationPreference:
    prefs = db.scalar(select(DealerNotificationPreference).where(DealerNotificationPreference.client_id == client_id))
    if prefs is None:
        prefs = DealerNotificationPreference(client_id=client_id)
        db.add(prefs)
        db.flush()
    return prefs


def recipients(db: Session, client_id: int, *, assigned_user_id: int | None, escalation_reasons: list[str] | None = None,
               kind: str = "ESCALATION") -> tuple[list[User], list[str]]:
    """Users (in-app + email) and extra email-only addresses that should be notified."""
    prefs = get_prefs(db, client_id)
    dealer_users = list(db.scalars(select(User).where(User.client_id == client_id, User.active.is_(True))))
    users: dict[int, User] = {}
    if assigned_user_id and prefs.notify_assigned_user:
        u = db.get(User, assigned_user_id)
        # Assignee must belong to this dealership, or be agency staff (staff work in every client's workspace).
        if u and u.active and (u.client_id == client_id or u.client_id is None):
            users[u.id] = u
    admins_needed = (not users) or kind != "ESCALATION" or bool(
        set(escalation_reasons or []) & set(prefs.escalate_to_admins_reasons or []))
    if admins_needed:
        for u in dealer_users:
            if u.client_role == "dealer_admin":
                users[u.id] = u
    extra: list[str] = []
    if kind == "ESCALATION":
        settings = db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == client_id))
        extra = [e for e in (settings.escalation_emails if settings else []) if e]
    return list(users.values()), extra


def _add(db: Session, client_id: int, **kw) -> DealerNotification | None:
    key = kw.get("idempotency_key")
    if key and db.scalar(select(DealerNotification.id).where(DealerNotification.idempotency_key == key)):
        return None
    n = DealerNotification(client_id=client_id, **kw)
    db.add(n)
    db.flush()
    if n.channel == "EMAIL" and n.status == "PENDING":
        enqueue(db, "customer.notify_email", f"customer.notify_email:{n.id}", {"notification_id": n.id}, max_attempts=4)
    return n


def notify(db: Session, client_id: int, *, kind: str, subject: str, body: str, link: str | None = None,
           assigned_user_id: int | None = None, escalation_reasons: list[str] | None = None,
           dedupe_key: str | None = None, immediate: bool = True) -> list[DealerNotification]:
    prefs = get_prefs(db, client_id)
    users, extra = recipients(db, client_id, assigned_user_id=assigned_user_id, escalation_reasons=escalation_reasons, kind=kind)
    base = get_settings().base_url.rstrip("/")
    full_link = f"{base}{link}" if link and link.startswith("/") else link
    created: list[DealerNotification] = []
    for u in users:
        n = _add(db, client_id, user_id=u.id, channel="IN_APP", kind=kind, subject=subject, body=body, link=link,
                 status="DELIVERED", sent_at=utcnow(), idempotency_key=f"{dedupe_key}:inapp:{u.id}" if dedupe_key else None)
        if n:
            created.append(n)
    want_email = prefs.email_enabled and ((immediate and (prefs.immediate_alerts or kind == "ESCALATION")) or kind == "DIGEST")
    for user_id, address in [(u.id, u.email) for u in users] + [(None, e) for e in extra]:
        n = _add(db, client_id, user_id=user_id, recipient_address=address, channel="EMAIL", kind=kind, subject=subject,
                 body=body + (f"\n\nOpen: {full_link}" if full_link else ""), link=link,
                 status="PENDING" if want_email else "SKIPPED",
                 status_detail=None if want_email else "email alerts disabled by preference",
                 idempotency_key=f"{dedupe_key}:email:{address}" if dedupe_key else None)
        if n:
            created.append(n)
    return created


def notify_operator(db: Session, client_id: int, *, kind: str, subject: str, body: str, link: str | None = None,
                    dedupe_key: str | None = None) -> list[DealerNotification]:
    """In-app notice to agency admins about a dealership's conversation (e.g. a delivery that needs reconciling)."""
    created = []
    for u in db.scalars(select(User).where(User.client_id.is_(None), User.role == "admin", User.active.is_(True))):
        n = _add(db, client_id, user_id=u.id, channel="IN_APP", kind=kind, subject=subject, body=body, link=link,
                 status="DELIVERED", sent_at=utcnow(), idempotency_key=f"{dedupe_key}:op:{u.id}" if dedupe_key else None)
        if n:
            created.append(n)
    return created


@on_failure("customer.notify_email")
def _mark_failed(db: Session, payload: dict, error: str) -> None:
    n = db.get(DealerNotification, payload.get("notification_id"))
    if n and n.status in ("PENDING", "SENDING"):
        n.status, n.status_detail = "FAILED", (error or "")[:500]
        audit.event(db, "notification.failed", client_id=n.client_id, entity_type="dealer_notification", entity_id=n.id)


def _deliver(db: Session, client: Client, n: DealerNotification) -> tuple[SendResult, bool]:
    """Returns (result, live). Classifies provider exceptions conservatively: anything that may have reached
    the provider is UNCERTAIN, never retried automatically."""
    from .. import client_email

    s = get_settings()
    live, _why = client_email.delivery_allowed(db, client)
    try:
        outcome = client_email.send(db, client, to=n.recipient_address, subject=f"[{s.business_name or 'dealerflow'}] {n.subject}",
                                    body=n.body, key=f"dealer-notification:{n.id}")
    except (ExternalActionsHalted, IntegrationDisabled, CredentialError) as exc:
        return SendResult("FAILED", error=str(exc), retryable=isinstance(exc, ExternalActionsHalted)), live
    except RateLimitedError as exc:
        return SendResult("FAILED", error=str(exc), retryable=True), live
    except PermanentError as exc:  # the provider rejected the request outright (4xx): definitively not delivered
        return SendResult("FAILED", error=str(exc)), live
    except IntegrationError as exc:
        return SendResult("FAILED", error=f"provider error after the request was sent: {exc}", uncertain=True), live
    if outcome == "sent":
        return SendResult("SENT"), True
    if outcome == "simulated":
        return SendResult("SIMULATED"), False
    return SendResult("NOT_SENT", error=outcome.removeprefix("not_sent: ")), False


@handler("customer.notify_email")
def send_email_job(db: Session, payload: dict) -> dict:
    from . import send_gate

    nid = payload["notification_id"]
    n = db.get(DealerNotification, nid)
    if n is None:
        return {"skipped": True}
    if n.status == "SENDING":
        verdict, wait, attempt = send_gate.check_sending_row(db, KIND, n.id)
        if verdict != "crashed":
            raise DeferJob(utcnow() + timedelta(seconds=wait or 30), "another worker is sending (or just sent) this notification")
        if not send_gate.take_over_crashed(db, attempt, DealerNotification, "status", n.id):
            return {"skipped": "owner finished"}
        db.refresh(n)
        n.status_detail = "worker stopped mid-send; not re-sent automatically"
        return {"uncertain": True}
    if not n.recipient_address:
        n.status, n.status_detail = "SKIPPED", "no email address"
        return {"skipped": True}
    if db.execute(update(DealerNotification).where(DealerNotification.id == nid, DealerNotification.status == "PENDING")
                  .values(status="SENDING").execution_options(synchronize_session=False)).rowcount != 1:
        return {"skipped": n.status}
    db.refresh(n)
    client = db.get(Client, n.client_id)
    from .. import client_email

    live, _ = client_email.delivery_allowed(db, client)
    gate = send_gate.open_attempt(db, kind=KIND, ref_id=n.id, channel="EMAIL", recipient=n.recipient_address, live=live,
                                  client_id=n.client_id)
    if not gate.allowed:
        n.status, n.status_detail = "BLOCKED", "; ".join(gate.reasons)
        return {"blocked": gate.reasons}
    n.attempts += 1
    db.commit()  # claim + ledger IN_FLIGHT are durable before the provider sees anything
    result, live = _deliver(db, client, n)
    if result.status == "NOT_SENT":
        gate.attempt.status, gate.attempt.error = "BLOCKED", result.error
        n.status, n.status_detail = "NOT_SENT", result.error
        return {"not_sent": result.error}
    send_gate.close_attempt(db, gate.attempt, result)
    if result.ok:
        n.status, n.sent_at = result.status, utcnow()
        n.status_detail = None if result.status == "SENT" else "SIMULATED: the email provider is the mock; nothing was emailed"
        return {"status": n.status}
    n.status_detail = result.error
    if result.uncertain:
        n.status = "UNCERTAIN"
        return {"uncertain": result.error}
    if result.retryable:
        n.status = "PENDING"
        db.commit()
        raise RateLimitedError(result.error or "transient email failure")
    n.status = "FAILED"
    audit.event(db, "notification.failed", client_id=n.client_id, entity_type="dealer_notification", entity_id=n.id)
    return {"failed": result.error}


# --- daily digest ---------------------------------------------------------------------------------------


def build_digest(db: Session, client: Client) -> tuple[str, str]:
    since = utcnow() - timedelta(days=1)
    new_convs = db.scalar(select(func.count(CustomerConversation.id)).where(
        CustomerConversation.client_id == client.id, CustomerConversation.created_at >= since)) or 0
    open_esc = list(db.scalars(select(Escalation).where(Escalation.client_id == client.id, Escalation.status != "RESOLVED").limit(20)))
    pending = db.scalar(select(func.count(ResponseDraft.id)).where(
        ResponseDraft.client_id == client.id, ResponseDraft.status == "PENDING_REVIEW")) or 0
    appts = list(db.scalars(select(TestDriveAppointment).where(
        TestDriveAppointment.client_id == client.id, TestDriveAppointment.status.in_(["REQUESTED", "PENDING_CONFIRMATION"]))))
    lines = [f"Daily summary for {client.name}", "",
             f"New conversations (24h): {new_convs}", f"Drafts awaiting review: {pending}",
             f"Open escalations: {len(open_esc)}", f"Appointments awaiting confirmation: {len(appts)}", ""]
    for e in open_esc:
        lines.append(f"- Escalation #{e.id} ({', '.join(e.reasons)}): {e.recommended_action}")
    return f"Daily summary — {client.name}", "\n".join(lines)


@handler("customer.daily_digests")
def daily_digests_job(db: Session, payload: dict) -> dict:
    sent = 0
    for c in db.scalars(select(Client).where(Client.status.in_(("active", "onboarding")))):
        prefs = get_prefs(db, c.id)
        if not prefs.daily_digest:
            continue
        local = utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(c.timezone))
        if local.hour != prefs.digest_hour_local:
            continue
        subject, body = build_digest(db, c)
        notify(db, c.id, kind="DIGEST", subject=subject, body=body, link=f"/dealers/{c.id}/inbox",
               dedupe_key=f"digest:{c.id}:{local.date().isoformat()}")
        sent += 1
    return {"digests": sent}


def schedule(db: Session, now) -> None:
    """Hourly digest check (each client's digest goes out once, at its local digest hour)."""
    enqueue(db, "customer.daily_digests", f"customer.daily_digests:{now:%Y-%m-%dT%H}", {}, max_attempts=2)
    enqueue(db, "customer.appt_reminder_sweep", f"customer.appt_reminder_sweep:{now:%Y-%m-%dT%H}", {}, max_attempts=2)
