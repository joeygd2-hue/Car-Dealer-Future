"""Configurable service catalog (Phase 4, Step 5).

The seeded packages and prices are PROPOSALS, not validated market prices.
Prices live only in the database and are edited through the catalog UI/API;
nothing else in the application hardcodes them.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import User
from app.models.finance import ServicePackage
from app.services import audit

PROPOSED_PACKAGES = [
    {
        "code": "listing_ops",
        "name": "Listing Operations",
        "description": "Inventory imports, listing generation and review, supported publication workflows, inventory synchronization.",
        "features": ["Inventory imports", "Listing generation (EN/ES)", "Listing review & approval", "Supported publication workflows", "Inventory synchronization"],
        "includes_packages": [],
        "setup_fee_cents": 0,
        "recurring_fee_cents": 0,
    },
    {
        "code": "bilingual_lead_response",
        "name": "Bilingual Lead Response",
        "description": "Connected-channel inquiry assistance with approved responses, lead summaries, appointment workflows and human escalation.",
        "features": ["Connected-channel inquiry assistance", "Approved responses (EN/ES)", "Lead summaries", "Appointment workflows", "Human escalation"],
        "includes_packages": [],
        "setup_fee_cents": 0,
        "recurring_fee_cents": 0,
    },
    {
        "code": "dealership_growth",
        "name": "Dealership Growth",
        "description": "Listing Operations + Bilingual Lead Response + SEO audits and reports + monthly performance reporting.",
        "features": ["SEO audits & reports", "Monthly performance reporting"],
        "includes_packages": ["listing_ops", "bilingual_lead_response"],
        "setup_fee_cents": 0,
        "recurring_fee_cents": 0,
    },
]


def seed_packages(db: Session) -> int:
    """Create the proposed packages with $0 placeholder prices; an admin must set real prices."""
    added = 0
    for spec in PROPOSED_PACKAGES:
        if db.scalar(select(ServicePackage.id).where(ServicePackage.code == spec["code"])):
            continue
        db.add(ServicePackage(**spec, billing_frequency="monthly", pricing_status="proposed"))
        added += 1
    db.flush()
    return added


def all_features(db: Session, pkg: ServicePackage, _seen: set | None = None) -> list[str]:
    _seen = _seen or set()
    feats = list(pkg.features)
    for code in pkg.includes_packages:
        if code in _seen:
            continue
        _seen.add(code)
        child = db.scalar(select(ServicePackage).where(ServicePackage.code == code))
        if child:
            feats = all_features(db, child, _seen) + feats
    return feats


EDITABLE = {"name", "description", "features", "setup_fee_cents", "recurring_fee_cents", "billing_frequency", "active", "pricing_status", "includes_packages"}


def update_package(db: Session, pkg: ServicePackage, changes: dict, actor: User) -> ServicePackage:
    before = {k: getattr(pkg, k) for k in EDITABLE}
    for k, v in changes.items():
        if k not in EDITABLE:
            raise ValueError(f"Field {k} is not editable")
        if k in ("setup_fee_cents", "recurring_fee_cents") and (not isinstance(v, int) or v < 0):
            raise ValueError(f"{k} must be a non-negative integer number of cents")
        if k == "billing_frequency" and v not in ("monthly", "quarterly", "annual", "one_time"):
            raise ValueError("Invalid billing frequency")
        if k == "pricing_status" and v not in ("proposed", "validated", "retired"):
            raise ValueError("Invalid pricing status")
        setattr(pkg, k, v)
    audit.record(db, "package.updated", actor=actor, entity=pkg, details={"before": before, "after": changes})
    return pkg
