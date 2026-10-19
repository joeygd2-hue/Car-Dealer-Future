"""Evaluation harness for the response engine (required before modes B or C can be authorized). Ported from Phase 3.

Runs every synthetic conversation in ``dealerflow/evals/synthetic_conversations.json`` through the real ingest →
process pipeline against an isolated in-memory SQLite database (no tenant data, nothing leaves the process), then
measures:
  factual_accuracy    — no unsupported numbers/claims, required facts present, forbidden content absent
  escalation_recall   — every case that must escalate did (must be 100%)
  escalation_accuracy — escalate/no-escalate decision correct
  reason_accuracy     — expected escalation reasons present
  language_accuracy   — reply in the customer's language
  opt_out_accuracy    — opt-outs stop all replies (must be 100%)
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...db import Base, utcnow
from ...models import Appointment, Client, Conversation, ConversationMessage, ResponseDraft, ResponseEvaluationRun, User
from ...models_dealer import InventorySource, Vehicle
from .common import log, utc_to_local
from .responder.engine import ENGINE_VERSION, T, describe_when
from .responder.facts import fmt_money, full_address, vehicle_title
from .responder.language import detect_language
from .responder.safety import validate_output

EVAL_PATH = Path(__file__).resolve().parents[2] / "evals" / "synthetic_conversations.json"
THRESHOLDS = {"factual_accuracy": 1.0, "escalation_recall": 1.0, "escalation_accuracy": 0.95, "reason_accuracy": 0.95,
              "language_accuracy": 0.95, "opt_out_accuracy": 1.0}
STATUS = {"AVAILABLE": "available", "PENDING": "pending", "SOLD": "sold"}


def load_cases(path: Path = EVAL_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed(db: Session, fixture: dict) -> Client:
    d = dict(fixture["dealership"])
    client = Client(name=d["name"], slug="eval", status="active", timezone=d.get("timezone", "America/Chicago"),
                    phone=d.get("phone"), address_line=d.get("address_line1"), city=d.get("city"), state=d.get("state"),
                    postal_code=d.get("postal_code"), business_hours={k: v for k, v in fixture["hours"].items()},
                    business_info_approved_at=utcnow(), simulated=True)
    db.add(client)
    db.flush()
    src = InventorySource(client_id=client.id, name="Evaluation fixture", source_type="manual", full_snapshot=False)
    db.add(src)
    db.flush()
    for i, v in enumerate(fixture["vehicles"]):
        v = dict(v)
        conflict = v.pop("data_conflict_note", None)
        issues = [{"code": "price_source_conflict", "field": "price", "severity": "error", "message": conflict}] if conflict else []
        db.add(Vehicle(client_id=client.id, source_id=src.id, external_id=v.get("stock_number") or f"eval-{i}", vin=v.get("vin"),
                       stock_number=v.get("stock_number"), year=v.get("year"), make=v.get("make"), model=v.get("model"),
                       trim=v.get("trim"), mileage=v.get("mileage"), price_cents=v.get("price_cents"),
                       inventory_status=STATUS[v["status"]], features=v.get("approved_features") or [],
                       validation_status="warnings" if issues else "valid", validation_issues=issues,
                       content_fingerprint=f"eval-{i}", photo_fingerprint="", simulated=True,
                       last_seen_at=utcnow() - timedelta(hours=v["verified_hours_ago"])))
    db.flush()
    return client


def run_cases(data: dict | None = None) -> dict:
    from . import outbound
    from .inbox import ingest_inbound, process_message

    data = data or load_cases()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda c, r: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    client = _seed(db, data["fixture"])
    allowed_base: list[str] = []
    for v in db.scalars(select(Vehicle)):
        allowed_base += [str(v.year), v.stock_number or ""]
    tallies = {k: [0, 0] for k in THRESHOLDS}
    failures, results = [], []
    for case in data["cases"]:
        conv_key = f"eval-{case['id']}"
        for i, text in enumerate(case["messages"]):
            res = ingest_inbound(db, client=client, channel="website_chat", external_conversation_id=conv_key, text=text,
                                 provider_message_id=f"{conv_key}-{i}", simulated=True)
            process_message(db, res.message.id)
            draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == res.conversation.id)
                              .order_by(ResponseDraft.id.desc()))
            if draft and draft.in_reply_to_message_id == res.message.id and i < len(case["messages"]) - 1:
                # Simulate staff approving the draft so multi-turn context includes our reply.
                draft.status = "sent"
                outbound.queue_outbound(db, res.conversation, draft.body, sender_type="staff", draft=draft)
            db.flush()
        conv = db.scalar(select(Conversation).where(Conversation.external_conversation_id == conv_key))
        last_inbound = db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conv.id,
                                                                    ConversationMessage.direction == "inbound")
                                  .order_by(ConversationMessage.id.desc())).first()
        draft = db.scalar(select(ResponseDraft).where(ResponseDraft.in_reply_to_message_id == last_inbound.id))
        body = draft.body if draft else ""
        problems: list[str] = []
        lang = case.get("lang", "en")
        if "opt_out" in case:
            tallies["opt_out_accuracy"][1] += 1
            ok = conv.opted_out == case["opt_out"] and (not case["opt_out"] or draft is None)
            tallies["opt_out_accuracy"][0] += ok
            if not ok:
                problems.append(f"opt_out expected {case['opt_out']} got {conv.opted_out} (draft={'yes' if draft else 'no'})")
        if draft is not None and not case.get("opt_out"):
            tallies["factual_accuracy"][1] += 1
            fact_values = []
            v = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
            if v is not None:
                fact_values += [vehicle_title(v), ", ".join(v.features or []), fmt_money(v.price_cents) if v.price_cents else "",
                                f"{v.mileage:,}" if v.mileage else ""]
            fact_values += [client.phone or "", full_address(client) or "", "9:00 AM 7:00 PM 6:00 PM 9:00 19:00 18:00"]
            appt = db.scalar(select(Appointment).where(Appointment.conversation_id == conv.id))
            if appt and appt.requested_start:
                fact_values.append(describe_when(utc_to_local(appt.requested_start, client.timezone), lang))
            allowed = fact_values + allowed_base + [x[lang] for x in T.values()]
            violations = validate_output(body, allowed_sources=allowed, language=lang)
            if v is not None and v.validation_issues and "$" in body:
                violations.append("quoted a price for a vehicle with conflicting data")
            low = body.lower()
            violations += [f"missing expected text: {s}" for s in case.get("must_include", []) if s.lower() not in low]
            violations += [f"contains forbidden text: {s}" for s in case.get("must_not_include", []) if s.lower() in low]
            fact_ok = not violations
            tallies["factual_accuracy"][0] += fact_ok
            problems += violations
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
                    tallies["factual_accuracy"][0] -= fact_ok
        elif not case.get("opt_out"):
            problems.append("no draft generated")
            tallies["factual_accuracy"][1] += 1
        results.append({"id": case["id"], "reply": body, "problems": problems})
        if problems:
            failures.append({"id": case["id"], "reply": body, "problems": problems})
    db.close()
    engine.dispose()
    metrics = {k: (round(n / d, 4) if d else 1.0) for k, (n, d) in tallies.items()}
    return {"metrics": metrics, "counts": tallies, "failures": failures, "results": results,
            "passed": all(metrics[k] >= t for k, t in THRESHOLDS.items()), "total": len(data["cases"])}


def run_and_record(db: Session, *, user: User | None, client_id: int | None = None) -> ResponseEvaluationRun:
    out = run_cases()
    run = ResponseEvaluationRun(client_id=client_id, engine_version=ENGINE_VERSION, total_cases=out["total"],
                                metrics={**out["metrics"], "thresholds": THRESHOLDS}, failures=out["failures"],
                                passed=out["passed"], triggered_by_id=user.id if user else None)
    db.add(run)
    db.flush()
    log(db, "conversation.evaluation_run", client_id=client_id or 0, entity_type="response_evaluation_run", entity_id=run.id,
        actor_id=user.id if user else None, actor_type="user" if user else "system", passed=run.passed, metrics=out["metrics"])
    return run
