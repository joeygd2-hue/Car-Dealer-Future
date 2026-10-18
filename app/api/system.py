from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db, platform_user
from app.config import settings
from app.core.ai import ai_configured
from app.core.jobs import queue_stats
from app.core.worker import worker_health
from app.models import Job, User

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    workers = worker_health(db)
    return {"status": "ok", "database": "ok",
            "worker": "running" if any(w["status"] in ("idle", "running") for w in workers) else "not_running"}


@router.get("/system/status")
def system_status(user: User = Depends(platform_user), db: Session = Depends(get_db)):
    dead = db.scalars(select(Job).where(Job.status.in_(("dead", "failed"))).order_by(Job.updated_at.desc()).limit(20)).all()
    return {
        "workers": worker_health(db),
        "queue": queue_stats(db),
        "recent_failed_jobs": [{"id": j.id, "type": j.type, "status": j.status, "attempts": j.attempts, "error": (j.last_error or "")[:300],
                                "at": j.updated_at.isoformat()} for j in dead],
        "integrations": {
            "ai_listing_generation": "enabled" if settings.ai_available else ("key present, disabled" if ai_configured() else "not configured"),
            "meta_live_publishing_allowed": settings.meta_live_publishing_allowed,
            "outreach_live_allowed": settings.outreach_live_allowed,
            "encryption_key_configured": bool(settings.encryption_key),
        },
    }
