"""Staff notifications for customer conversations (ported from Phase 3 ``services/notifications.py``).

Every notification is stored in-app for the recipients, with the real delivery status. New leads, escalations and
test-drive requests are also e-mailed to the dealership's contact address through Phase 4's
``client_email.send``, which is SIMULATED unless client e-mail is explicitly enabled and authorized for that
client. Notifications never contain secrets; they link to the conversation instead of copying contact details.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...integrations.errors import ExternalActionsHalted, IntegrationError
from ...models import Client, StaffNotification, User

EMAIL_KINDS = ("escalation", "appointment", "new_lead")


def recipients(db: Session, client_id: int, assigned_user_id: int | None) -> list[User]:
    if assigned_user_id:
        u = db.get(User, assigned_user_id)
        if u and u.active and (u.client_id in (None, client_id)):
            return [u]
    return list(db.scalars(select(User).where(User.client_id == client_id, User.active.is_(True),
                                              User.client_role.in_(("dealer_admin", "dealer_reviewer")))))


def notify(db: Session, client_id: int, *, kind: str, subject: str, body: str, link: str | None,
           dedupe_key: str, assigned_user_id: int | None = None) -> list[StaffNotification]:
    """Idempotent per ``dedupe_key``: calling again with the same key creates nothing new."""
    out: list[StaffNotification] = []
    for user in recipients(db, client_id, assigned_user_id):
        key = f"{dedupe_key}:u{user.id}"
        if db.scalar(select(StaffNotification.id).where(StaffNotification.idempotency_key == key)):
            continue
        row = StaffNotification(client_id=client_id, user_id=user.id, channel="in_app", kind=kind, subject=subject[:300],
                                body=body, link=link, status="stored", idempotency_key=key)
        db.add(row)
        out.append(row)
    if kind in EMAIL_KINDS:
        key = f"{dedupe_key}:email"
        if not db.scalar(select(StaffNotification.id).where(StaffNotification.idempotency_key == key)):
            client = db.get(Client, client_id)
            row = StaffNotification(client_id=client_id, channel="email", kind=kind, subject=subject[:300], body=body,
                                    link=link, idempotency_key=key)
            if not client or not client.contact_email:
                row.status, row.status_detail = "not_sent", "dealership has no contact e-mail on file"
            else:
                from ..client_email import send

                try:
                    outcome = send(db, client, to=client.contact_email, subject=subject, body=f"{body}\n\nOpen: {link or ''}",
                                   key=f"staffnote:{key}")
                    row.status = outcome.split(":", 1)[0]
                    row.status_detail = outcome
                except ExternalActionsHalted as exc:
                    row.status, row.status_detail = "not_sent", str(exc)
                except IntegrationError as exc:
                    row.status, row.status_detail = "failed", str(exc)[:500]
            db.add(row)
            out.append(row)
    db.flush()
    return out
