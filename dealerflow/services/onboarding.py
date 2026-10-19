"""Client onboarding checklist and automation gating (Phase 4, Step 6)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import AutomationSetting, Client, IntegrationConnection, OnboardingStep, User
from .. import audit

STEPS = [
    ("create_workspace", "Create client workspace"),
    ("record_services", "Record agreed services"),
    ("business_details", "Collect business details"),
    ("authorizations", "Obtain required authorizations"),
    ("inventory_sources", "Connect approved inventory sources"),
    ("comms_integrations", "Configure email, messaging and calendar integrations"),
    ("inventory_validated", "Import and validate inventory"),
    ("hours_and_rules", "Configure business hours and approved response rules"),
    ("workflow_tests", "Test the listing and inquiry workflows"),
    ("permissions_confirmed", "Confirm required permissions"),
    ("client_approval", "Obtain client approval"),
    ("service_activated", "Activate the agreed service"),
    ("initial_report", "Generate the initial report"),
    ("results_review", "Review results with the client"),
]
STEP_LABELS = dict(STEPS)

# Which onboarding steps must be complete (done or not_applicable) before an automation may be enabled,
# plus which integration kinds must be connected (not just sandbox) for live operation.
AUTOMATION_PREREQS: dict[str, dict] = {
    "inventory_sync": {
        "label": "Automatic inventory synchronization",
        "steps": ["authorizations", "inventory_sources", "inventory_validated", "permissions_confirmed", "client_approval"],
        "integrations": ["inventory_feed"],
    },
    "listing_publish": {
        "label": "Automatic listing publication (supported channels only)",
        "steps": ["authorizations", "inventory_validated", "workflow_tests", "permissions_confirmed", "client_approval"],
        "integrations": [],
    },
    "inquiry_auto_response": {
        "label": "Automatic approved-response sending to customer inquiries",
        "steps": ["authorizations", "comms_integrations", "hours_and_rules", "workflow_tests", "permissions_confirmed", "client_approval"],
        "integrations": ["customer_messaging"],
    },
    "seo_monitoring": {
        "label": "Scheduled SEO audits",
        "steps": ["authorizations", "business_details", "client_approval"],
        "integrations": [],
    },
    "monthly_report_email": {
        "label": "Monthly report emails (each report needs manager approval before it is sent)",
        "steps": ["business_details", "client_approval", "initial_report"],
        "integrations": ["report_email"],
    },
}

COMPLETE = ("done", "not_applicable")


def ensure_checklist(db: Session, client: Client) -> list[OnboardingStep]:
    existing = {s.code: s for s in db.scalars(select(OnboardingStep).where(OnboardingStep.client_id == client.id))}
    for pos, (code, _label) in enumerate(STEPS, start=1):
        if code not in existing:
            step = OnboardingStep(client_id=client.id, code=code, position=pos, status="pending")
            if code == "create_workspace":
                step.status, step.completed_at = "done", utcnow()
            db.add(step)
    db.flush()
    return checklist(db, client.id)


def checklist(db: Session, client_id: int) -> list[OnboardingStep]:
    return list(db.scalars(select(OnboardingStep).where(OnboardingStep.client_id == client_id).order_by(OnboardingStep.position)))


def set_step(db: Session, client_id: int, code: str, status: str, actor: User, notes: str | None = None, evidence: str | None = None) -> OnboardingStep:
    if code not in STEP_LABELS:
        raise ValueError("Unknown onboarding step")
    if status not in ("pending", "in_progress", "done", "blocked", "not_applicable"):
        raise ValueError("Invalid status")
    step = db.scalar(select(OnboardingStep).where(OnboardingStep.client_id == client_id, OnboardingStep.code == code))
    if step is None:
        raise ValueError("Checklist not initialised")
    if code == "service_activated" and status == "done":
        missing = [STEP_LABELS[c] for c, _ in STEPS[:11] if not _is_complete(db, client_id, c)]
        if missing:
            raise ValueError("Cannot activate service. Outstanding prerequisites: " + "; ".join(missing))
    if status == "done" and code in ("authorizations", "client_approval", "permissions_confirmed") and not (evidence or step.evidence):
        raise ValueError("Record evidence (e.g. signed authorization reference) before marking this step done")
    old = step.status
    step.status = status
    step.notes = notes if notes is not None else step.notes
    step.evidence = evidence if evidence is not None else step.evidence
    if status in COMPLETE:
        step.completed_at, step.completed_by_id = utcnow(), actor.id
    else:
        step.completed_at = None
    if code == "service_activated" and status == "done":
        client = db.get(Client, client_id)
        client.status, client.activated_at = "active", utcnow()
    audit.record(db, "onboarding.step", actor=actor, client_id=client_id, entity=step, details={"code": code, "from": old, "to": status})
    return step


def _is_complete(db: Session, client_id: int, code: str) -> bool:
    return db.scalar(
        select(OnboardingStep.id).where(OnboardingStep.client_id == client_id, OnboardingStep.code == code, OnboardingStep.status.in_(COMPLETE))
    ) is not None


def outstanding_prereqs(db: Session, client_id: int, feature: str) -> list[str]:
    spec = AUTOMATION_PREREQS.get(feature)
    if spec is None:
        return [f"Unknown automation '{feature}'"]
    missing = [f"Onboarding: {STEP_LABELS[c]}" for c in spec["steps"] if not _is_complete(db, client_id, c)]
    for kind in spec["integrations"]:
        conn = db.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.client_id == client_id, IntegrationConnection.kind == kind, IntegrationConnection.status == "connected"
            )
        )
        if conn is None:
            missing.append(f"Integration not connected and verified: {kind}")
    client = db.get(Client, client_id)
    if client and client.status != "active":
        missing.append("Client service is not activated")
    return missing


def set_automation(db: Session, client_id: int, feature: str, enabled: bool, actor: User) -> AutomationSetting:
    if enabled:
        missing = outstanding_prereqs(db, client_id, feature)
        if missing:
            raise ValueError("Automation blocked. Outstanding prerequisites: " + "; ".join(missing))
    row = db.scalar(select(AutomationSetting).where(AutomationSetting.client_id == client_id, AutomationSetting.feature == feature))
    if row is None:
        row = AutomationSetting(client_id=client_id, feature=feature)
        db.add(row)
    row.enabled = enabled
    row.enabled_by_id = actor.id if enabled else None
    row.enabled_at = utcnow() if enabled else None
    db.flush()
    audit.record(db, "automation." + ("enabled" if enabled else "disabled"), actor=actor, client_id=client_id, details={"feature": feature})
    return row


def automation_enabled(db: Session, client_id: int, feature: str) -> bool:
    row = db.scalar(select(AutomationSetting).where(AutomationSetting.client_id == client_id, AutomationSetting.feature == feature))
    return bool(row and row.enabled)
