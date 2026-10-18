"""Evaluation harness for the response engine (required before enabling Mode B or C).

Runs every synthetic conversation in ``evals/synthetic_conversations.json`` through the real ingest →
process pipeline against an isolated in-memory database (no tenant data), then measures:
  factual_accuracy   — no unsupported numbers/claims, required facts present, forbidden content absent
  escalation_recall  — every case that must escalate did (must be 100%)
  escalation_accuracy— escalate/no-escalate decision correct
  reason_accuracy    — expected escalation reasons present
  language_accuracy  — reply in the customer's language
  opt_out_accuracy   — opt-outs stop all replies (must be 100%)
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, utcnow
from app.models import Appointment, BusinessHours, Conversation, Dealership, EvaluationRun, ResponseDraft, Vehicle
from app.services.responder.engine import ENGINE_VERSION
from app.services.responder.language import detect_language
from app.services.responder.safety import validate_output

EVAL_PATH = Path(__file__).resolve().parents[2] / "evals" / "synthetic_conversations.json"
THRESHOLDS = {"factual_accuracy": 1.0, "escalation_recall": 1.0, "escalation_accuracy": 0.95, "reason_accuracy": 0.95,
              "language_accuracy": 0.95, "opt_out_accuracy": 1.0}


def load_cases(path: Path = EVAL_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed(db: Session, fixture: dict) -> Dealership:
    d = Dealership(slug="eval", status="ACTIVE", business_info_approved=True, **fixture["dealership"])
    db.add(d)
    db.flush()
    for wd, h in fixture["hours"].items():
        db.add(BusinessHours(dealership_id=d.id, weekday=int(wd), closed=h is None,
                             open_time=h[0] if h else None, close_time=h[1] if h else None))
    for v in fixture["vehicles"]:
        v = dict(v)
        hours_ago = v.pop("verified_hours_ago")
        db.add(Vehicle(dealership_id=d.id, verified_at=utcnow() - timedelta(hours=hours_ago), **v))
    db.flush()
    db.refresh(d)
    return d


def run_cases(data: dict | None = None) -> dict:
    from app.services.inbox.processing import ingest_inbound, process_message

    data = data or load_cases()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    import app.models  # noqa: F401

    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    dealership = _seed(db, data["fixture"])
    allowed_base = []
    for v in db.scalars(select(Vehicle)):
        allowed_base += [str(v.year), v.stock_number or ""]
    tallies = {k: [0, 0] for k in THRESHOLDS}
    failures = []
    results = []
    for case in data["cases"]:
        conv_key = f"eval-{case['id']}"
        draft = None
        for i, text in enumerate(case["messages"]):
            res = ingest_inbound(db, dealership=dealership, channel="WEBSITE_CHAT", external_conversation_id=conv_key,
                                 text=text, provider_message_id=f"{conv_key}-{i}")
            process_message(db, res.message.id)
            # Simulate staff approving the draft so multi-turn context includes our reply.
            draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == res.conversation.id)
                              .order_by(ResponseDraft.id.desc()))
            if draft and draft.in_reply_to_message_id == res.message.id and i < len(case["messages"]) - 1:
                from app.services.inbox.outbound import queue_outbound

                draft.status = "SENT"
                queue_outbound(db, res.conversation, draft.body, sender_type="STAFF", draft=draft)
            db.flush()
        conv = db.scalar(select(Conversation).where(Conversation.external_conversation_id == conv_key))
        db.refresh(conv)
        last_inbound = [m for m in conv.messages if m.direction == "INBOUND"][-1]
        draft = db.scalar(select(ResponseDraft).where(ResponseDraft.in_reply_to_message_id == last_inbound.id))
        body = draft.body if draft else ""
        problems = []
        lang = case.get("lang", "en")

        if "opt_out" in case:
            tallies["opt_out_accuracy"][1] += 1
            ok = conv.opted_out == case["opt_out"] and (not case["opt_out"] or draft is None)
            tallies["opt_out_accuracy"][0] += ok
            if not ok:
                problems.append(f"opt_out expected {case['opt_out']} got {conv.opted_out} (draft={'yes' if draft else 'no'})")
        if draft is not None and not case.get("opt_out"):
            # Factual accuracy.
            tallies["factual_accuracy"][1] += 1
            fact_values = []
            v = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
            if v is not None:
                from app.services.responder.facts import fmt_money

                fact_values += [v.title, ", ".join(v.approved_features or []), fmt_money(v.price_cents) if v.price_cents else "", f"{v.mileage:,}" if v.mileage else ""]
            fact_values += [dealership.phone, dealership.full_address, "9:00 AM 7:00 PM 6:00 PM 9:00 19:00 18:00"]
            appt = db.scalar(select(Appointment).where(Appointment.conversation_id == conv.id))
            from app.services.appointments import utc_to_local
            from app.services.responder.engine import T, describe_when

            if appt:
                fact_values.append(describe_when(utc_to_local(appt.requested_start, dealership.timezone), lang))
            allowed = fact_values + allowed_base + [x[lang] for x in T.values()]
            violations = validate_output(body, allowed_sources=allowed, language=lang)
            if v is not None and v.data_conflict_note and ("$" in body):
                violations.append("quoted a price for a vehicle with conflicting data")
            low = body.lower()
            for s in case.get("must_include", []):
                if s.lower() not in low:
                    violations.append(f"missing expected text: {s}")
            for s in case.get("must_not_include", []):
                if s.lower() in low:
                    violations.append(f"contains forbidden text: {s}")
            fact_ok = not violations
            tallies["factual_accuracy"][0] += fact_ok
            problems += violations
            # Escalation.
            if "escalate" in case:
                escalated = bool(draft.escalation_reasons)
                tallies["escalation_accuracy"][1] += 1
                tallies["escalation_accuracy"][0] += escalated == case["escalate"]
                if case["escalate"]:
                    tallies["escalation_recall"][1] += 1
                    tallies["escalation_recall"][0] += escalated
                if escalated != case["escalate"]:
                    problems.append(f"escalate expected {case['escalate']} got {escalated} {draft.escalation_reasons}")
            if case.get("reasons"):
                tallies["reason_accuracy"][1] += 1
                missing = [r for r in case["reasons"] if r not in draft.escalation_reasons]
                tallies["reason_accuracy"][0] += not missing
                if missing:
                    problems.append(f"missing escalation reasons {missing} (got {draft.escalation_reasons})")
            # Language.
            tallies["language_accuracy"][1] += 1
            detected, _ = detect_language(body, prior=None)
            lang_ok = detected == lang and draft.language == lang
            tallies["language_accuracy"][0] += lang_ok
            if not lang_ok:
                problems.append(f"language expected {lang} got {detected}/{draft.language}")
            if "appointment" in case:
                has = appt is not None
                if has != case["appointment"]:
                    problems.append(f"appointment expected {case['appointment']} got {has}")
                    tallies["factual_accuracy"][0] -= fact_ok  # count as a factual/workflow failure
        elif not case.get("opt_out"):
            problems.append("no draft generated")
            tallies["factual_accuracy"][1] += 1
        results.append({"id": case["id"], "reply": body, "problems": problems})
        if problems:
            failures.append({"id": case["id"], "reply": body, "problems": problems})
    db.close()
    metrics = {k: (round(n / d, 4) if d else 1.0) for k, (n, d) in tallies.items()}
    passed = all(metrics[k] >= t for k, t in THRESHOLDS.items())
    return {"metrics": metrics, "counts": tallies, "failures": failures, "results": results, "passed": passed,
            "total": len(data["cases"])}


def run_and_record(db: Session, *, user_id: int | None, dealership_id: int | None = None) -> EvaluationRun:
    out = run_cases()
    run = EvaluationRun(dealership_id=dealership_id, engine_version=ENGINE_VERSION, total_cases=out["total"],
                        metrics={**out["metrics"], "thresholds": THRESHOLDS}, failures=out["failures"],
                        passed=out["passed"], triggered_by_id=user_id)
    db.add(run)
    db.flush()
    from app.services import audit

    audit.record(db, "evaluation.run", actor_type="USER" if user_id else "SYSTEM", actor_user_id=user_id,
                 dealership_id=dealership_id, entity_type="evaluation_run", entity_id=run.id, passed=run.passed,
                 metrics=out["metrics"])
    return run
