"""Evaluation harness for the response engine (required before a dealership can enable Mode B or C).

Runs every synthetic conversation in ``fixtures/evals/synthetic_conversations.json`` through the real ingest →
process pipeline against an isolated in-memory database (no tenant data, no providers), then measures:
  factual_accuracy    no unsupported numbers/claims, required facts present, forbidden content absent
  escalation_recall   every case that must escalate did (must be 100%)
  escalation_accuracy escalate/no-escalate decision correct
  reason_accuracy     expected escalation reasons present
  language_accuracy   reply in the customer's language
  opt_out_accuracy    opt-outs stop all replies (must be 100%)
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...db import Base, utcnow
from ...models import Appointment, Client, Conversation, EvaluationRun, InventorySource, ResponseDraft, Vehicle
from .common import record, utc_to_local
from .responder.engine import ENGINE_VERSION, T, describe_when
from .responder.facts import fmt_money, full_address, vehicle_title
from .responder.language import detect_language
from .responder.safety import validate_output

EVAL_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "evals" / "synthetic_conversations.json"
THRESHOLDS = {"factual_accuracy": 1.0, "escalation_recall": 1.0, "escalation_accuracy": 0.95, "reason_accuracy": 0.95,
              "language_accuracy": 0.95, "opt_out_accuracy": 1.0}


def load_cases(path: Path = EVAL_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed(db: Session, fixture: dict) -> Client:
    d = dict(fixture["dealership"])
    client = Client(name=d["name"], phone=d.get("phone"), address_line=d.get("address_line1"), city=d.get("city"),
                    state=d.get("state"), postal_code=d.get("postal_code"), timezone=d.get("timezone", "America/Chicago"),
                    status="active", simulated=True, business_hours=fixture["hours"], business_info_approved_at=utcnow())
    db.add(client)
    db.flush()
    src = InventorySource(client_id=client.id, name="Evaluation fixture", source_type="manual")
    db.add(src)
    db.flush()
    for v in fixture["vehicles"]:
        v = dict(v)
        seen = utcnow() - timedelta(hours=v.pop("verified_hours_ago"))
        db.add(Vehicle(client_id=client.id, source_id=src.id, external_id=v["stock_number"], vin=v.get("vin"),
                       stock_number=v["stock_number"], year=v.get("year"), make=v.get("make"), model=v.get("model"),
                       trim=v.get("trim"), price_cents=v.get("price_cents"), mileage=v.get("mileage"),
                       inventory_status=v["status"].lower(), features=v.get("approved_features") or [],
                       data_conflict_note=v.get("data_conflict_note"), content_fingerprint="eval",
                       photo_fingerprint="eval", first_seen_at=seen, last_seen_at=seen, last_synced_at=seen,
                       simulated=True))
    db.flush()
    return client


def _isolated_session() -> Session:
    import dealerflow.models  # noqa: F401  (register every table)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # pragma: no cover - trivial
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def run_cases(data: dict | None = None) -> dict:
    from .inbox import ingest_inbound, process_message
    from .outbound import queue_outbound

    data = data or load_cases()
    db = _isolated_session()
    client = _seed(db, data["fixture"])
    allowed_base = []
    for v in db.scalars(select(Vehicle)):
        allowed_base += [str(v.year), v.stock_number or ""]
    tallies = {k: [0, 0] for k in THRESHOLDS}
    failures, results = [], []
    for case in data["cases"]:
        conv_key = f"eval-{case['id']}"
        for i, text in enumerate(case["messages"]):
            res = ingest_inbound(db, client=client, channel="WEBSITE_CHAT", external_conversation_id=conv_key, text=text,
                                 provider_message_id=f"{conv_key}-{i}")
            process_message(db, res.message.id)
            # Simulate staff approving the draft so multi-turn context includes our reply.
            draft = db.scalar(select(ResponseDraft).where(ResponseDraft.conversation_id == res.conversation.id)
                              .order_by(ResponseDraft.id.desc()))
            if draft and draft.in_reply_to_message_id == res.message.id and i < len(case["messages"]) - 1:
                draft.status = "SENT"
                queue_outbound(db, res.conversation, draft.body, sender_type="STAFF", draft=draft)
            db.flush()
        conv = db.scalar(select(Conversation).where(Conversation.external_conversation_id == conv_key))
        db.refresh(conv)
        last_inbound = [m for m in conv.messages if m.direction == "INBOUND"][-1]
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
            fact_values: list[str] = []
            v = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
            if v is not None:
                fact_values += [vehicle_title(v), ", ".join(v.features or []),
                                fmt_money(v.price_cents) if v.price_cents else "", f"{v.mileage:,}" if v.mileage else ""]
            fact_values += [client.phone or "", full_address(client) or "", "9:00 AM 7:00 PM 6:00 PM 9:00 19:00 18:00"]
            appt = db.scalar(select(Appointment).where(Appointment.conversation_id == conv.id))
            if appt:
                fact_values.append(describe_when(utc_to_local(appt.requested_start, client.timezone), lang))
            allowed = fact_values + allowed_base + [x[lang] for x in T.values()]
            violations = validate_output(body, allowed_sources=allowed, language=lang)
            if v is not None and v.data_conflict_note and "$" in body:
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
            if "appointment" in case and (appt is not None) != case["appointment"]:
                problems.append(f"appointment expected {case['appointment']} got {appt is not None}")
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


def run_and_record(db: Session, *, user_id: int | None, client_id: int | None = None) -> EvaluationRun:
    out = run_cases()
    run = EvaluationRun(client_id=client_id, engine_version=ENGINE_VERSION, total_cases=out["total"],
                        metrics={**out["metrics"], "thresholds": THRESHOLDS}, failures=out["failures"],
                        passed=out["passed"], triggered_by_id=user_id)
    db.add(run)
    db.flush()
    record(db, "evaluation.run", client_id=client_id, actor_id=user_id, entity_type="evaluation_run", entity_id=run.id,
           passed=run.passed, metrics=out["metrics"])
    return run
