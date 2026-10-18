"""Data retention, client export/deletion, attachments, bootstrap and system jobs."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Base
from app.models.base import utcnow
from app.models.core import Attachment, Client, User
from app.models.dealer_ops import Conversation, ConversationMessage
from app.models.outreach import InboundMessage
from app.services import audit
from app.services.jobs import ensure_scheduled_task, handler

ALLOWED_UPLOAD_TYPES = {
    "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "text/csv": ".csv", "text/plain": ".txt",
}
MAGIC = {".pdf": b"%PDF", ".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff", ".webp": b"RIFF"}


def store_attachment(db: Session, *, owner_type: str, owner_id: int, filename: str, content_type: str, data: bytes,
                     actor: User | None, client_id: int | None = None) -> Attachment:
    s = get_settings()
    if len(data) == 0:
        raise ValueError("Empty file")
    if len(data) > s.max_upload_bytes:
        raise ValueError(f"File exceeds {s.max_upload_bytes // (1024 * 1024)} MB limit")
    ext = ALLOWED_UPLOAD_TYPES.get(content_type)
    if ext is None:
        raise ValueError("Unsupported file type (allowed: PDF, PNG, JPEG, WEBP, CSV, TXT)")
    if ext in MAGIC and not data.startswith(MAGIC[ext]):
        raise ValueError("File content does not match its declared type")
    key = f"{owner_type}/{uuid.uuid4().hex}{ext}"
    path = os.path.join(s.attachments_dir, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    safe_name = os.path.basename(filename).replace("\x00", "")[:255] or f"upload{ext}"
    att = Attachment(owner_type=owner_type, owner_id=owner_id, client_id=client_id, filename=safe_name, content_type=content_type,
                     size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), storage_key=key, uploaded_by_user_id=actor.id if actor else None)
    db.add(att)
    db.flush()
    return att


def attachment_path(att: Attachment) -> str:
    root = os.path.abspath(get_settings().attachments_dir)
    path = os.path.abspath(os.path.join(root, att.storage_key))
    if not path.startswith(root + os.sep):
        raise ValueError("Invalid attachment path")
    return path


def purge_conversation_pii(db: Session, older_than_days: int = 365) -> int:
    """Remove customer contact details and message bodies from old conversations; keep counts for reporting."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    ids = [c.id for c in db.scalars(select(Conversation).where(Conversation.created_at < cutoff, Conversation.pii_purged_at.is_(None)))]
    if not ids:
        return 0
    db.execute(update(Conversation).where(Conversation.id.in_(ids)).values(customer_name=None, customer_contact=None, summary="[purged]", pii_purged_at=utcnow()))
    db.execute(update(ConversationMessage).where(ConversationMessage.conversation_id.in_(ids)).values(body="[purged]"))
    db.execute(update(InboundMessage).where(InboundMessage.received_at < cutoff).values(body="[purged]"))
    audit.record(db, "retention.purged_conversations", details={"count": len(ids), "older_than_days": older_than_days})
    return len(ids)


def export_client(db: Session, client_id: int) -> dict:
    """Every row owned by the client, for data-portability requests or pre-deletion backup."""
    out: dict[str, list] = {}
    for table in Base.metadata.sorted_tables:
        if "client_id" not in table.c:
            continue
        rows = db.execute(select(table).where(table.c.client_id == client_id)).mappings().all()
        if rows:
            out[table.name] = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items() if k not in ("password_hash",)} for r in rows]
    return out


def delete_client(db: Session, client_id: int, confirm_name: str, actor: User, export_dir: str) -> str:
    """Hard-delete a client after writing a JSON export. Requires the exact client name as confirmation."""
    client = db.get(Client, client_id)
    if client is None:
        raise ValueError("Client not found")
    if confirm_name != client.name:
        raise ValueError("Confirmation name does not match")
    os.makedirs(export_dir, exist_ok=True)
    path = os.path.join(export_dir, f"client-{client_id}-{utcnow():%Y%m%d%H%M%S}.json")
    with open(path, "w") as fh:
        json.dump(export_client(db, client_id), fh, default=str)
    audit.record(db, "client.deleted", actor=actor, client_id=None, details={"client_id": client_id, "name": client.name, "export": path})
    db.execute(delete(Client).where(Client.id == client_id))  # ON DELETE CASCADE removes owned rows
    return path


def bootstrap(db: Session) -> None:
    """Idempotent seed of reference data and recurring tasks."""
    from app.services import costs, finance, packages
    from app.services.agent.state import get_config

    finance.seed_expense_categories(db)
    packages.seed_packages(db)
    costs.seed_default_rates(db)
    get_config(db)
    db.commit()
    ensure_scheduled_task(db, "agent-tick", "agent.tick", 300)
    ensure_scheduled_task(db, "budget-check", "costs.check_budgets", 3600)
    ensure_scheduled_task(db, "monthly-reports", "reports.monthly", 86400)
    ensure_scheduled_task(db, "retention-purge", "maintenance.retention", 86400)


@handler("costs.check_budgets")
def _job_budgets(db: Session, job) -> dict:
    from app.services.costs import check_budgets

    return {"alerts": check_budgets(db)}


@handler("maintenance.retention")
def _job_retention(db: Session, job) -> dict:
    return {"purged": purge_conversation_pii(db, int(job.payload.get("days", 365)))}


@handler("reports.monthly")
def _job_monthly_reports(db: Session, job) -> dict:
    """On the first days of a month, draft last month's report for each active client (and send if automated)."""
    from app.models.seo import ClientReport
    from app.services.onboarding import automation_enabled
    from app.services.reports import generate_report, previous_month, send_report

    today = utcnow().date()
    start, end = previous_month(today)
    made = 0
    for client in db.scalars(select(Client).where(Client.status == "active")):
        if db.scalar(select(ClientReport.id).where(ClientReport.client_id == client.id, ClientReport.period_start == start)):
            continue
        r = generate_report(db, client, start, end, None)
        made += 1
        if automation_enabled(db, client.id, "monthly_report_email") and client.contact_email:
            system = db.scalar(select(User).where(User.role == "owner").limit(1))
            if system:
                send_report(db, r, client.contact_email, system)
    return {"reports": made, "period": f"{start}..{end}"}


@handler("stripe.process_event")
def _job_stripe(db: Session, job) -> dict:
    from app.integrations.stripe_webhooks import process
    from app.models.core import WebhookEvent

    row = db.get(WebhookEvent, job.payload["webhook_event_id"])
    if row is None or row.status == "processed":
        return {"skipped": True}
    try:
        return {"result": process(db, row)}
    except Exception as exc:
        db.rollback()
        row = db.get(WebhookEvent, job.payload["webhook_event_id"])
        row.status, row.error = "failed", str(exc)[:2000]
        db.commit()
        raise


def today() -> date:
    return utcnow().date()
