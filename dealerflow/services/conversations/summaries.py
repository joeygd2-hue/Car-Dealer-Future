"""Staff-facing conversation summaries (deterministic, built only from stored records)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...models import Appointment, Conversation, CustomerContact, Escalation, ResponseDraft, Vehicle
from .facts import fmt_money, vehicle_status, vehicle_title

TOPIC_ACTIONS = {
    "financing": "Answer the financing question personally; do not commit to terms or approval in writing.",
    "credit": "Discuss credit options personally with a finance manager.",
    "warranty": "Confirm warranty/service-contract details from dealership documents before replying.",
    "trade_in": "Arrange a trade-in appraisal; do not quote a value without inspection.",
    "negotiation": "A salesperson/manager should handle price discussion within dealership rules.",
    "condition_history": "Share the vehicle history report / inspection details after verifying them.",
    "complaint": "Manager should contact the customer directly to resolve the complaint.",
    "human_request": "Contact the customer personally as soon as possible.",
    "frustration": "Respond personally and promptly; acknowledge the customer's concern.",
    "sensitive_data": "Contact the customer through a secure channel; do not request sensitive data in chat.",
    "uncertain_inventory": "Verify current price/availability in inventory, update the record, then reply.",
    "missing_data": "Add the missing information (hours, address, features or mileage) and reply.",
    "unknown_question": "Answer the customer's question personally.",
    "appointment_change": "Contact the customer to cancel or reschedule the appointment.",
    "low_confidence": "Review the draft carefully before sending.",
    "ambiguous_vehicle": "Confirm which vehicle the customer means (several match), then reply.",
}

INTENT_LABELS = {
    "availability": "availability", "price": "price", "mileage": "mileage", "features": "features",
    "hours": "hours", "location": "location", "test_drive": "test drive", "financing": "financing",
    "credit": "credit", "warranty": "warranty", "trade_in": "trade-in", "negotiation": "discount/negotiation",
    "condition_history": "vehicle history/condition", "complaint": "complaint", "human_request": "wants a human",
    "cancel_appointment": "appointment change", "unknown_question": "other question",
}


def recommended_next_step(conv: Conversation, open_esc: Escalation | None, pending_draft: ResponseDraft | None,
                          pending_appt: Appointment | None) -> str:
    if conv.opted_out:
        return "Do not contact: the customer opted out. Only respond if they reach out again."
    if open_esc:
        for reason in open_esc.reasons:
            if reason in TOPIC_ACTIONS:
                return TOPIC_ACTIONS[reason]
    if pending_appt:
        return "Confirm or decline the requested test drive time in the Appointments page."
    if pending_draft:
        return "Review, edit if needed, and send the drafted reply."
    return "No action needed right now; waiting on the customer."


def build_summary(db: Session, conv: Conversation) -> dict:
    db.flush()
    db.expire(conv, ["messages"])
    vehicle = db.get(Vehicle, conv.vehicle_id) if conv.vehicle_id else None
    contact = db.get(CustomerContact, conv.contact_id) if conv.contact_id else None
    inbound = [m for m in conv.messages if m.direction == "INBOUND"]
    intents: list[str] = []
    questions: list[str] = []
    features: set[str] = set()
    times: list[str] = []
    for m in inbound:
        an = m.analysis or {}
        for i in an.get("intents", []):
            if i in INTENT_LABELS and INTENT_LABELS[i] not in intents:
                intents.append(INTENT_LABELS[i])
        questions += an.get("questions", [])
        features |= set(an.get("requested_features", []))
        if an.get("requested_time_text"):
            times.append(an["requested_time_text"])
    drafts = list(db.scalars(select(ResponseDraft).where(ResponseDraft.conversation_id == conv.id).order_by(ResponseDraft.id)))
    verified: list[str] = []
    for d in drafts:
        if d.status in ("SENT", "AUTO_SENT"):
            for f in d.facts_used:
                if f == "price" and vehicle and vehicle.price_cents:
                    item = f"Listed price {fmt_money(vehicle.price_cents)}"
                elif f == "mileage" and vehicle and vehicle.mileage is not None:
                    item = f"Mileage {vehicle.mileage:,}"
                elif f == "availability" and vehicle:
                    item = f"Availability: {vehicle_status(vehicle) or 'unknown'}"
                else:
                    item = f.replace("_", " ")
                if item not in verified:
                    verified.append(item)
    pending_draft = next((d for d in reversed(drafts) if d.status == "PENDING_REVIEW"), None)
    latest = drafts[-1] if drafts else None
    open_esc = db.scalar(select(Escalation).where(Escalation.conversation_id == conv.id, Escalation.status != "RESOLVED")
                         .order_by(Escalation.id.desc()))
    appts = list(db.scalars(select(Appointment).where(Appointment.conversation_id == conv.id).order_by(Appointment.id)))
    pending_appt = next((a for a in appts if a.status in ("REQUESTED", "PENDING_CONFIRMATION", "RESCHEDULE_REQUESTED")), None)
    unresolved = list(latest.unresolved) if latest and latest.status == "PENDING_REVIEW" else []
    if open_esc:
        unresolved += [q for q in questions[-3:] if q not in unresolved]
    contact_details = {}
    if contact:
        for k in ("display_name", "phone", "email"):
            if getattr(contact, k):
                contact_details[k] = getattr(contact, k)
    summary = {
        "vehicle_of_interest": vehicle_title(vehicle) + (f" (stock {vehicle.stock_number})" if vehicle and vehicle.stock_number else "")
        if vehicle else None,
        "preferred_language": conv.preferred_language,
        "main_questions": intents,
        "recent_questions": questions[-5:],
        "verified_information_provided": verified,
        "customer_preferences": {"requested_features": sorted(features), "requested_times": times},
        "contact_details": contact_details,
        "test_drive_interest": any(i == "test drive" for i in intents) or bool(appts),
        "appointment_status": appts[-1].status if appts else "NONE",
        "unresolved_questions": unresolved,
        "escalation_reasons": open_esc.reasons if open_esc else [],
        "recommended_next_step": recommended_next_step(conv, open_esc, pending_draft, pending_appt),
        "conversation_link": f"/dealers/{conv.client_id}/conversations/{conv.id}",
        "opted_out": conv.opted_out,
    }
    conv.summary = summary
    conv.summary_updated_at = utcnow()
    conv.unresolved_questions = unresolved
    conv.next_action = summary["recommended_next_step"]
    conv.appointment_status = summary["appointment_status"]
    return summary


def summary_text(summary: dict) -> str:
    lines = [
        f"Vehicle: {summary.get('vehicle_of_interest') or 'not identified'}",
        f"Language: {'Spanish' if summary.get('preferred_language') == 'es' else 'English'}",
        f"Topics: {', '.join(summary.get('main_questions') or []) or '—'}",
    ]
    if summary.get("verified_information_provided"):
        lines.append("Already told customer: " + "; ".join(summary["verified_information_provided"]))
    if summary.get("unresolved_questions"):
        lines.append("Unresolved: " + "; ".join(summary["unresolved_questions"]))
    if summary.get("escalation_reasons"):
        lines.append("Escalation: " + ", ".join(summary["escalation_reasons"]))
    lines.append(f"Next step: {summary.get('recommended_next_step')}")
    return "\n".join(lines)


def deliver_lead_summary(db: Session, conv: Conversation) -> str:
    """Email the structured summary to the dealership's contact address (Phase 4 client email gate applies).

    Returns 'sent', 'simulated', 'already_delivered' or 'not_sent: <reason>'. Customer contact details are included
    only as the customer provided them; nothing is added that is not in the conversation record."""
    from ...models import Client
    from .. import client_email
    from . import runtime

    client = db.get(Client, conv.client_id)
    if conv.summary_delivered_at is not None:
        return "already_delivered"
    if not client.contact_email:
        return "not_sent: the client has no contact email"
    summary = build_summary(db, conv)
    contact = summary.get("contact_details") or {}
    body = (summary_text(summary)
            + ("\nCustomer: " + ", ".join(str(v) for v in contact.values()) if contact else "")
            + f"\nOpen the conversation: {summary['conversation_link']}")
    outcome = client_email.send(db, client, to=client.contact_email, subject=f"Lead summary: conversation #{conv.id}",
                                body=body, key=f"conv_summary:{conv.id}")
    if outcome in ("sent", "simulated"):
        conv.summary_delivered_at = utcnow()
    runtime.record(db, "conversation.summary_delivered", client_id=conv.client_id, entity_type="conversation",
                   entity_id=conv.id, outcome={"sent": "OK", "simulated": "SIMULATED"}.get(outcome, "FAILED"),
                   external=outcome == "sent", delivery=outcome)
    return outcome
