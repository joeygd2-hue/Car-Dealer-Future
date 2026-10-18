"""Pipeline stages, timeline activities and tasks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import utcnow
from ..models import DEFAULT_STAGES, Activity, PipelineStage, Prospect, Task, User
from ..security import role_at_least

# Stages that may only be entered through the workflow that proves them, never by a manual move.
WORKFLOW_ONLY = {
    "outreach_approved": "Approve a drafted message in the outreach queue.",
    "contacted": "Set automatically when a message is actually sent.",
    "replied": "Set automatically when a reply is imported and matched.",
    "meeting_booked": "Set automatically when the calendar and Zoom providers confirm the booking.",
}
# Do-not-contact can be set by anyone; leaving it needs an admin.


class StageError(ValueError):
    pass


def seed_stages(db: Session) -> None:
    existing = {s.key for s in db.scalars(select(PipelineStage))}
    for pos, (key, label, terminal) in enumerate(DEFAULT_STAGES):
        if key not in existing:
            db.add(PipelineStage(key=key, label=label, position=pos, is_terminal=terminal))
    db.flush()


def stages(db: Session, *, active_only: bool = True) -> list[PipelineStage]:
    q = select(PipelineStage).order_by(PipelineStage.position)
    if active_only:
        q = q.where(PipelineStage.active.is_(True))
    return list(db.scalars(q))


def stage_labels(db: Session) -> dict[str, str]:
    return {s.key: s.label for s in stages(db, active_only=False)}


def add_activity(db: Session, prospect: Prospect | int, kind: str, summary: str, *, body: str = "",
                 actor_id: int | None = None, ref_type: str | None = None, ref_id: int | None = None,
                 data: dict[str, Any] | None = None, at: datetime | None = None) -> Activity:
    pid = prospect if isinstance(prospect, int) else prospect.id
    act = Activity(prospect_id=pid, kind=kind, summary=summary[:500], body=body, actor_id=actor_id,
                   ref_type=ref_type, ref_id=ref_id, data=data, created_at=at or utcnow())
    db.add(act)
    return act


def set_stage(db: Session, prospect: Prospect, new_stage: str, *, actor: User | None = None,
              reason: str = "", workflow: bool = False) -> bool:
    """Move a prospect. ``workflow=True`` is used by the services that prove workflow-only stages."""
    if new_stage == prospect.stage:
        return False
    known = {s.key for s in stages(db, active_only=False)}
    if new_stage not in known:
        raise StageError(f"Unknown stage {new_stage!r}")
    if not workflow and new_stage in WORKFLOW_ONLY:
        raise StageError(f"'{new_stage}' cannot be set manually. {WORKFLOW_ONLY[new_stage]}")
    if prospect.do_not_contact and new_stage != "do_not_contact":
        if workflow or actor is None or not role_at_least(actor.role, "admin"):
            raise StageError("Prospect is marked do-not-contact; only an admin can change that.")
        prospect.do_not_contact = False
    if new_stage == "do_not_contact":
        prospect.do_not_contact = True
    old = prospect.stage
    prospect.stage = new_stage
    labels = stage_labels(db)
    add_activity(db, prospect, "stage", f"Stage: {labels.get(old, old)} → {labels.get(new_stage, new_stage)}",
                 body=reason, actor_id=actor.id if actor else None, data={"from": old, "to": new_stage})
    audit(db, "prospect.stage", actor_id=actor.id if actor else None, entity_type="prospect", entity_id=prospect.id,
          data={"from": old, "to": new_stage, "reason": reason, "workflow": workflow})
    return True


def advance_stage(db: Session, prospect: Prospect, new_stage: str, *, reason: str = "") -> bool:
    """Workflow move that only goes forward in the pipeline (never out of won/lost/do-not-contact)."""
    order = {key: i for i, (key, _, _) in enumerate(DEFAULT_STAGES)}
    if prospect.do_not_contact or prospect.stage in ("won", "lost", "do_not_contact"):
        return False
    if prospect.stage != "follow_up_later" and order.get(new_stage, 0) <= order.get(prospect.stage, 0):
        return False
    return set_stage(db, prospect, new_stage, reason=reason, workflow=True)


def create_task(db: Session, title: str, due_at: datetime, *, prospect: Prospect | None = None, kind: str = "follow_up",
                assignee_id: int | None = None, created_by_id: int | None = None, notes: str = "",
                dedupe_key: str | None = None) -> Task:
    """Create a task. With ``dedupe_key`` the same automatic task is never created twice."""
    if dedupe_key:
        existing = db.scalar(select(Task).where(Task.dedupe_key == dedupe_key))
        if existing:
            return existing
    task = Task(title=title[:255], due_at=due_at, prospect_id=prospect.id if prospect else None, kind=kind,
                assignee_id=assignee_id if assignee_id is not None else (prospect.owner_id if prospect else None),
                created_by_id=created_by_id, notes=notes, dedupe_key=dedupe_key)
    try:
        with db.begin_nested():
            db.add(task)
            db.flush()
    except IntegrityError:
        found = db.scalar(select(Task).where(Task.dedupe_key == dedupe_key))
        assert found is not None
        return found
    if prospect:
        add_activity(db, prospect, "task", f"Task created: {title}", actor_id=created_by_id, ref_type="task",
                     ref_id=task.id, data={"due_at": due_at.isoformat()})
    return task


def complete_task(db: Session, task: Task, *, actor: User | None = None) -> None:
    if task.status != "open":
        return
    task.status, task.completed_at = "done", utcnow()
    if task.prospect_id:
        add_activity(db, task.prospect_id, "task", f"Task done: {task.title}", actor_id=actor.id if actor else None,
                     ref_type="task", ref_id=task.id)
    audit(db, "task.complete", actor_id=actor.id if actor else None, entity_type="task", entity_id=task.id)


def default_follow_up_due(days: int = 3) -> datetime:
    return utcnow() + timedelta(days=days)
