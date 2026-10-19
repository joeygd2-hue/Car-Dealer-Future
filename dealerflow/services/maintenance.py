"""Phase 4 operations: attachments, data retention, client export/deletion, seeds and recurring jobs."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import Base, utcnow
from ..jobs import enqueue, handler
from ..models import Attachment, Client, ClientReport, Conversation, ConversationMessage, User

ALLOWED_UPLOAD_TYPES = {
    "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "text/csv": ".csv", "text/plain": ".txt",
}
MAGIC = {".pdf": b"%PDF", ".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff", ".webp": b"RIFF"}


def store_attachment(db: Session, *, owner_type: str, owner_id: int, filename: str, content_type: str, data: bytes,
                     actor: User | None) -> Attachment:
    s = get_settings()
    if not data:
        raise ValueError("Empty file")
    if len(data) > s.max_upload_bytes:
        raise ValueError(f"File exceeds the {s.max_upload_bytes // (1024 * 1024)} MB limit")
    ext = ALLOWED_UPLOAD_TYPES.get(content_type)
    if ext is None:
        raise ValueError("Unsupported file type (allowed: PDF, PNG, JPEG, WEBP, CSV, TXT)")
    if ext in MAGIC and not data.startswith(MAGIC[ext]):
        raise ValueError("File content does not match its declared type")
    key = f"{owner_type}/{uuid.uuid4().hex}{ext}"
    path = os.path.join(s.attachments_dir, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from ..crypto import encrypt_bytes

    with open(path, "wb") as fh:
        fh.write(encrypt_bytes(data))  # encrypted at rest (Fernet); see read_attachment
    safe_name = os.path.basename(filename.replace("\\", "/")).replace("\x00", "")[:255] or f"upload{ext}"
    att = Attachment(owner_type=owner_type, owner_id=owner_id, filename=safe_name, content_type=content_type, size_bytes=len(data),
                     sha256=hashlib.sha256(data).hexdigest(), storage_key=key, uploaded_by_id=actor.id if actor else None)
    db.add(att)
    db.flush()
    return att


def read_attachment(att: Attachment) -> bytes:
    """Decrypt a stored attachment and check it is the file that was uploaded (SHA-256 recorded at upload)."""
    from ..crypto import decrypt_bytes

    with open(attachment_path(att), "rb") as fh:
        data = decrypt_bytes(fh.read())  # files stored before encryption existed are returned as-is
    if hashlib.sha256(data).hexdigest() != att.sha256:
        raise ValueError("Stored attachment failed its integrity check")
    return data


def attachment_path(att: Attachment) -> str:
    root = os.path.abspath(get_settings().attachments_dir)
    path = os.path.abspath(os.path.join(root, att.storage_key))
    if not path.startswith(root + os.sep):
        raise ValueError("Invalid attachment path")
    return path


def purge_conversation_pii(db: Session, older_than_days: int) -> int:
    """Remove car-buyer contact details and message text from conversations idle longer than the retention period.

    Counts, statuses and timestamps stay for reporting; ``customer_suppressions`` (opt-outs) are kept so they are
    still honoured. A contact is cleared once none of its conversations is still within the retention period.
    """
    from sqlalchemy import func

    from ..models import Appointment, CustomerContact, CustomerIdentity, ResponseDraft

    cutoff = utcnow() - timedelta(days=older_than_days)
    last_activity = func.coalesce(Conversation.last_inbound_at, Conversation.created_at)
    ids = list(db.scalars(select(Conversation.id).where(last_activity < cutoff, Conversation.pii_purged_at.is_(None))))
    if not ids:
        return 0
    now = utcnow()
    db.execute(update(Conversation).where(Conversation.id.in_(ids)).values(
        summary={"purged": True}, unresolved_questions=[], next_action=None, pii_purged_at=now))
    db.execute(update(ConversationMessage).where(ConversationMessage.conversation_id.in_(ids)).values(body="[purged]", analysis={}))
    db.execute(update(ResponseDraft).where(ResponseDraft.conversation_id.in_(ids)).values(body="[purged]", fact_snapshot={}))
    db.execute(update(Appointment).where(Appointment.conversation_id.in_(ids)).values(
        customer_name=None, customer_phone=None, customer_email=None, notes=None))
    contact_ids = set(db.scalars(select(Conversation.contact_id).where(Conversation.id.in_(ids), Conversation.contact_id.is_not(None))))
    still_active = set(db.scalars(select(Conversation.contact_id).where(Conversation.contact_id.in_(contact_ids),
                                                                       Conversation.pii_purged_at.is_(None))))
    clear = contact_ids - still_active
    if clear:
        db.execute(update(CustomerContact).where(CustomerContact.id.in_(clear)).values(
            display_name=None, email=None, phone=None, pii_purged_at=now))
        # Identities can hold an email address (ADF leads); a returning customer starts a new contact.
        db.execute(delete(CustomerIdentity).where(CustomerIdentity.contact_id.in_(clear)))
    audit.record(db, "retention.purged_conversations", details={"count": len(ids), "contacts": len(clear),
                                                                "older_than_days": older_than_days})
    return len(ids)


def export_client(db: Session, client_id: int) -> dict:
    """Every row owned by the client (data-portability request or pre-deletion backup)."""
    out: dict[str, list] = {}
    client = db.get(Client, client_id)
    if client is None:
        raise ValueError("Client not found")
    out["clients"] = [{c.name: _jsonable(getattr(client, c.key)) for c in Client.__table__.columns}]
    for table in Base.metadata.sorted_tables:
        if "client_id" not in table.c or table.name == "clients":
            continue
        rows = db.execute(select(table).where(table.c.client_id == client_id)).mappings().all()
        if rows:
            out[table.name] = [{k: _jsonable(v) for k, v in r.items() if k not in ("password_hash", "api_key_hash")} for r in rows]
    return out


def _jsonable(v):
    return v.isoformat() if hasattr(v, "isoformat") else (float(v) if hasattr(v, "as_tuple") else v)


def delete_client(db: Session, client_id: int, confirm_name: str, actor: User, export_dir: str) -> str:
    """Write a JSON export, then hard-delete the client (owned rows cascade). Requires the exact name."""
    client = db.get(Client, client_id)
    if client is None:
        raise ValueError("Client not found")
    if confirm_name != client.name:
        raise ValueError("Confirmation name does not match the client name")
    os.makedirs(export_dir, exist_ok=True)
    path = os.path.join(export_dir, f"client-{client_id}-{utcnow():%Y%m%d%H%M%S}.json")
    with open(path, "w") as fh:
        json.dump(export_client(db, client_id), fh, default=str)
    audit.record(db, "client.deleted", actor=actor, details={"deleted_client_id": client_id, "name": client.name, "export": path})
    _delete_client_rows(db, client_id)
    return path


def _delete_client_rows(db: Session, client_id: int) -> None:
    """Delete every row the client owns, children first (Phase 2/3 tenant foreign keys do not cascade).

    Tables whose link to the client is ``ON DELETE SET NULL`` (e.g. expenses, cost entries) keep their rows without the
    link, as before. The client's own users are disabled and anonymised rather than deleted, because the audit log
    references them; their e-mail, name and credentials are removed.
    """
    import secrets as _secrets

    from ..models import User

    for table in reversed(Base.metadata.sorted_tables):
        if table.name in ("clients", "users") or "client_id" not in table.c:
            continue
        fk = next((f for f in table.c.client_id.foreign_keys if f.column.table.name == "clients"), None)
        if fk is not None and (fk.ondelete or "").upper() == "SET NULL" or table.c.client_id.nullable:
            db.execute(update(table).where(table.c.client_id == client_id).values(client_id=None))
        else:
            db.execute(delete(table).where(table.c.client_id == client_id))
    for u in db.scalars(select(User).where(User.client_id == client_id)):
        u.email, u.name = f"deleted-user-{u.id}@deleted.invalid", "deleted user"
        u.password_hash, u.api_key_hash, u.active = f"disabled${_secrets.token_hex(16)}", None, False
        u.client_id, u.client_role = None, "deleted"
    db.flush()
    db.execute(delete(Client).where(Client.id == client_id))


def seed_phase4(db: Session) -> None:
    """Idempotent reference data for Phase 4."""
    from . import finance, packages

    finance.seed_expense_categories(db)
    packages.seed_packages(db)


def schedule_daily_phase4(db: Session, now: datetime) -> None:
    """Called by the worker's daily scheduler; keys make every job once-per-day."""
    day = now.date().isoformat()
    enqueue(db, "check_budgets", f"check_budgets:{now:%Y-%m-%dT%H}", {}, max_attempts=2)  # hourly bucket
    enqueue(db, "retention_purge", f"retention_purge:{day}", {}, max_attempts=2)
    enqueue(db, "appointment_reminder_sweep", f"appointment_reminder_sweep:{now:%Y-%m-%dT%H}", {}, max_attempts=2)  # hourly
    enqueue(db, "rate_counter_purge", f"rate_counter_purge:{day}", {}, max_attempts=2)
    if now.day <= 3:
        enqueue(db, "monthly_reports", f"monthly_reports:{now:%Y-%m}", {}, max_attempts=3)


@handler("check_budgets")
def check_budgets_job(db: Session, payload: dict) -> dict:
    from .usage import check_budgets

    return {"alerts": check_budgets(db)}


@handler("rate_counter_purge")
def rate_counter_purge_job(db: Session, payload: dict) -> dict:
    from .rate_limits import purge_old

    return {"deleted": purge_old(db)}


@handler("retention_purge")
def retention_job(db: Session, payload: dict) -> dict:
    return {"purged": purge_conversation_pii(db, get_settings().retention_days_conversations)}


@handler("monthly_reports")
def monthly_reports_job(db: Session, payload: dict) -> dict:
    """Draft last month's report for each active client; email it only where that automation is enabled."""
    from .client_reports import generate_report, previous_month, send_report
    from .onboarding import automation_enabled

    start, end = previous_month(utcnow().date())
    made = 0
    for client in db.scalars(select(Client).where(Client.status == "active")):
        if db.scalar(select(ClientReport.id).where(ClientReport.client_id == client.id, ClientReport.period_start == start)):
            continue
        report = generate_report(db, client, start, end, None)
        made += 1
        if automation_enabled(db, client.id, "monthly_report_email") and client.contact_email:
            report.status = "final"
            send_report(db, report, client.contact_email, None)
    return {"reports": made, "period": f"{start}..{end}"}


@handler("stripe_event")
def stripe_event_job(db: Session, payload: dict) -> dict:
    from ..integrations.stripe import process
    from ..models import WebhookEvent

    row = db.get(WebhookEvent, payload["webhook_event_id"])
    if row is None or row.status == "processed":
        return {"skipped": True}
    return {"result": process(db, row)}
