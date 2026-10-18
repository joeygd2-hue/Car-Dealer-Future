"""Grounded bilingual reply composer.

The composer assembles a reply from (1) dealer-approved templates/FAQs and (2) fixed, reviewed
sentence frames filled ONLY with values from the verified FactSheet. Anything it cannot answer
from verified data becomes an "unresolved question" and an escalation reason. An optional LLM may
rephrase the result, but the rephrasing is discarded unless it passes ``validate_output``.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.services.responder import analysis as A
from app.services.responder.analysis import Analysis
from app.services.responder.facts import FactSheet, fmt_time, match_faq
from app.services.responder.safety import validate_output

ENGINE_VERSION = "1.0.0"

# Escalation reason codes (shown to staff).
R_FINANCING, R_CREDIT, R_WARRANTY, R_TRADE = "financing", "credit", "warranty", "trade_in"
R_NEGOTIATION, R_CONDITION, R_COMPLAINT = "negotiation", "condition_history", "complaint"
R_HUMAN, R_FRUSTRATION, R_SENSITIVE = "human_request", "frustration", "sensitive_data"
R_INVENTORY, R_MISSING, R_UNKNOWN, R_APPT_CHANGE = "uncertain_inventory", "missing_data", "unknown_question", "appointment_change"
R_LOW_CONFIDENCE = "low_confidence"
R_AMBIGUOUS = "ambiguous_vehicle"

INTENT_TO_REASON = {
    A.FINANCING: R_FINANCING, A.CREDIT: R_CREDIT, A.WARRANTY: R_WARRANTY, A.TRADE_IN: R_TRADE,
    A.NEGOTIATION: R_NEGOTIATION, A.CONDITION: R_CONDITION, A.COMPLAINT: R_COMPLAINT, A.HUMAN_REQUEST: R_HUMAN,
    A.UNKNOWN: R_UNKNOWN, A.CANCEL_APPOINTMENT: R_APPT_CHANGE,
}

T = {
    "greet": {"en": "Hi{name}! Thanks for reaching out to {dealer}.", "es": "¡Hola{name}! Gracias por comunicarse con {dealer}."},
    "avail_yes": {"en": "The {vehicle} is currently showing as available in our inventory.",
                  "es": "El {vehicle} aparece como disponible en nuestro inventario actual."},
    "avail_pending": {"en": "The {vehicle} currently has a sale pending, so I'll have a team member confirm its status for you.",
                      "es": "El {vehicle} tiene una venta pendiente, así que un miembro de nuestro equipo le confirmará su estado."},
    "avail_sold": {"en": "The {vehicle} has been sold. A team member can help you look at similar vehicles.",
                   "es": "El {vehicle} ya se vendió. Un miembro de nuestro equipo puede ayudarle a ver vehículos similares."},
    "price": {"en": "The listed price is {price}.", "es": "El precio publicado es de {price}."},
    "mileage": {"en": "Our inventory record shows {mileage} miles.", "es": "Nuestro registro de inventario indica {mileage} millas."},
    "features": {"en": "Approved features listed for this vehicle include: {features}.",
                 "es": "Las características aprobadas de este vehículo incluyen: {features}."},
    "feature_yes": {"en": "Yes, {feature} is listed among this vehicle's approved features.",
                    "es": "Sí, {feature} aparece entre las características aprobadas de este vehículo."},
    "hours": {"en": "Our hours are: {hours}.", "es": "Nuestro horario es: {hours}."},
    "location": {"en": "We're located at {address}.", "es": "Estamos ubicados en {address}."},
    "which_vehicle": {"en": "Which vehicle are you interested in? A link, stock number, or the year, make and model would help.",
                      "es": "¿Qué vehículo le interesa? Un enlace, el número de inventario o el año, marca y modelo me ayudarían."},
    "ambiguous_vehicle": {"en": "We have more than one vehicle that matches. Could you share the year, trim, or stock number?",
                          "es": "Tenemos más de un vehículo que coincide. ¿Podría indicarme el año, la versión o el número de inventario?"},
    "confirm_needed": {"en": "I need to confirm {what} with our team, and someone will follow up with you shortly.",
                       "es": "Necesito confirmar {what} con nuestro equipo y alguien le dará seguimiento pronto."},
    "topic_human": {"en": "Questions about {topic} need to be handled by a member of our team. I've passed your question along, and someone will follow up with you.",
                    "es": "Las preguntas sobre {topic} las atiende directamente un miembro de nuestro equipo. Ya pasé su pregunta y alguien se comunicará con usted."},
    "human": {"en": "Of course. I've asked a member of our team to contact you directly.",
              "es": "Por supuesto. Ya le pedí a un miembro de nuestro equipo que se comunique con usted directamente."},
    "sorry": {"en": "I'm sorry for the trouble. I've shared this with a manager, who will follow up with you directly.",
              "es": "Lamento mucho la molestia. Ya compartí esto con un gerente, quien se comunicará con usted directamente."},
    "sensitive": {"en": "For your security, please don't share sensitive personal information such as Social Security, bank, or card numbers here. A team member will follow up with you.",
                  "es": "Por su seguridad, por favor no comparta información personal delicada como números de Seguro Social, cuentas bancarias o tarjetas por este medio. Un miembro de nuestro equipo se comunicará con usted."},
    "unknown": {"en": "Thanks for your question. I want to make sure you get accurate information, so I've passed it to our team to confirm.",
                "es": "Gracias por su pregunta. Quiero asegurarme de darle información correcta, así que la pasé a nuestro equipo para confirmarla."},
    "td_offer": {"en": "Would you like to schedule a test drive?", "es": "¿Le gustaría agendar una prueba de manejo?"},
    "td_ask_time": {"en": "I'd be happy to help you set up a test drive. What day and time work best for you?",
                    "es": "Con gusto le ayudo a agendar una prueba de manejo. ¿Qué día y hora le funcionan mejor?"},
    "td_request": {"en": "I've sent your test drive request for {when} to our team. It isn't confirmed yet; a team member will confirm the time with you.",
                   "es": "Envié su solicitud de prueba de manejo para el {when} a nuestro equipo. Aún no está confirmada; un miembro del equipo le confirmará la hora."},
    "td_closed": {"en": "That time is outside our business hours ({hours}). What other day or time would work for you?",
                  "es": "Ese horario está fuera de nuestro horario de atención ({hours}). ¿Qué otro día u hora le funcionaría?"},
    "td_hours_unknown": {"en": "I'll have a team member confirm whether {when} works for a test drive.",
                         "es": "Un miembro de nuestro equipo le confirmará si el {when} funciona para una prueba de manejo."},
    "ask_contact": {"en": "What's the best name and phone number or email for our team to confirm with you?",
                    "es": "¿Cuál es el mejor nombre y número de teléfono o correo para que nuestro equipo le confirme?"},
    "appt_change": {"en": "Thanks for letting us know. I've passed your request to change your appointment to our team, and someone will follow up with you.",
                    "es": "Gracias por avisarnos. Pasé su solicitud de cambio de cita a nuestro equipo y alguien se comunicará con usted."},
    "thanks": {"en": "You're welcome! Let us know if there's anything else we can help with.",
               "es": "¡Con gusto! Avísenos si hay algo más en lo que podamos ayudarle."},
}

TOPIC = {
    "en": {R_FINANCING: "financing", R_CREDIT: "credit", R_WARRANTY: "warranties", R_TRADE: "trade-ins",
           R_NEGOTIATION: "pricing changes or discounts", R_CONDITION: "vehicle history and condition"},
    "es": {R_FINANCING: "financiamiento", R_CREDIT: "crédito", R_WARRANTY: "garantías", R_TRADE: "intercambios (trade-in)",
           R_NEGOTIATION: "cambios de precio o descuentos", R_CONDITION: "el historial y la condición del vehículo"},
}
CONFIRM_WHAT = {
    "en": {"availability": "the current availability", "price": "the current price", "mileage": "the mileage",
           "features": "the vehicle's features", "hours": "our hours", "location": "our address"},
    "es": {"availability": "la disponibilidad actual", "price": "el precio actual", "mileage": "el millaje",
           "features": "las características del vehículo", "hours": "nuestro horario", "location": "nuestra dirección"},
}
DAYS = {"en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]}


@dataclass
class ReplyPlan:
    body: str
    language: str
    intents: list[str]
    answered: list[str] = field(default_factory=list)
    template_covered: list[str] = field(default_factory=list)  # intents answered from approved templates/FAQs
    facts_used: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    escalation_reasons: list[str] = field(default_factory=list)
    confidence: float = 1.0
    no_reply: bool = False
    appointment_request: datetime | None = None  # local naive
    appointment_request_in_hours: bool | None = None
    needs_contact: bool = False
    generated_by: str = "template"
    validation_errors: list[str] = field(default_factory=list)


def _fill(text: str, values: dict[str, str | None]) -> str | None:
    fields = [f for _, f, _, _ in string.Formatter().parse(text) if f]
    if any(values.get(f) is None for f in fields):
        return None
    return text.format(**{f: values[f] for f in fields})


def describe_when(dt: datetime, lang: str) -> str:
    day = DAYS[lang][dt.weekday()]
    t = fmt_time(dt.strftime("%H:%M"), lang)
    if lang == "es":
        return f"{day} {dt.day}/{dt.month} a las {t}"
    return f"{day}, {dt:%b} {dt.day} at {t}"


def within_hours(fs: FactSheet, start: datetime, duration_minutes: int = 30) -> bool | None:
    if not fs.hours:
        return None
    window = fs.hours.get(start.weekday())
    if not window:
        return False
    open_h, close_h = window
    open_dt = datetime.combine(start.date(), datetime.strptime(open_h, "%H:%M").time())
    close_dt = datetime.combine(start.date(), datetime.strptime(close_h, "%H:%M").time())
    return open_dt <= start and start + timedelta(minutes=duration_minutes) <= close_dt


def compose(an: Analysis, fs: FactSheet, text: str, *, contact_name: str | None, has_contact_method: bool,
            already_offered_test_drive: bool, is_first_reply: bool, now_local: datetime) -> ReplyPlan:
    lang = fs.language
    plan = ReplyPlan(body="", language=lang, intents=list(an.intents))
    parts: list[str] = []
    values = {"dealer": fs.dealership_name, "dealer_name": fs.dealership_name, "vehicle": fs.vehicle_title,
              "price": fs.price_text, "mileage": fs.mileage_text, "features": ", ".join(fs.features) or None,
              "hours": fs.hours_text, "address": fs.address, "phone": fs.phone,
              "name": f" {contact_name}" if contact_name else ""}

    if an.opt_out:
        plan.no_reply = True
        plan.escalation_reasons = []
        return plan

    def say(key: str, **extra) -> None:
        parts.append(_fill(T[key][lang], {**values, **extra}) or "")

    def use_template(intent: str) -> bool:
        tpl = fs.templates.get(intent)
        if tpl:
            filled = _fill(tpl.body, values)
            if filled:
                parts.append(filled)
                plan.template_covered.append(intent)
                return True
        return False

    def unresolved(what_key: str, reason: str) -> None:
        plan.unresolved.append(what_key)
        if reason not in plan.escalation_reasons:
            plan.escalation_reasons.append(reason)
        parts.append(T["confirm_needed"][lang].format(what=CONFIRM_WHAT[lang][what_key]))

    # Safety signals first.
    if an.sensitive_data:
        plan.escalation_reasons.append(R_SENSITIVE)
    if an.frustration:
        plan.escalation_reasons.append(R_FRUSTRATION)
    for intent in an.intents:
        reason = INTENT_TO_REASON.get(intent)
        if reason and reason not in plan.escalation_reasons:
            plan.escalation_reasons.append(reason)

    if is_first_reply or A.GREETING in an.intents:
        if not use_template(A.GREETING):
            say("greet")

    if an.sensitive_data:
        say("sensitive")
    if A.COMPLAINT in an.intents or an.frustration:
        say("sorry")
    if an.human_request:
        say("human")

    faq = match_faq(fs, text)
    if faq:
        parts.append(faq.answer)
        plan.template_covered.append("faq")
        plan.answered.append("faq")
        plan.facts_used.append(f"faq:{faq.id}")

    vehicle_intents = [i for i in (A.AVAILABILITY, A.PRICE, A.MILEAGE, A.FEATURES) if i in an.intents]
    if vehicle_intents and fs.vehicle is None:
        say("ambiguous_vehicle" if fs.vehicle_ambiguous else "which_vehicle")
        plan.confidence -= 0.15
        if fs.vehicle_ambiguous:
            plan.unresolved.append("vehicle")
            plan.escalation_reasons.append(R_AMBIGUOUS)
    elif vehicle_intents:
        problems = fs.inventory_problems
        if A.AVAILABILITY in an.intents:
            if fs.availability == "AVAILABLE":
                if not use_template(A.AVAILABILITY):
                    say("avail_yes")
                plan.answered.append(A.AVAILABILITY)
                plan.facts_used.append("availability")
            elif fs.availability == "SOLD":
                say("avail_sold")
                plan.answered.append(A.AVAILABILITY)
                plan.facts_used.append("availability")
            elif fs.availability == "PENDING":
                say("avail_pending")
                plan.unresolved.append("availability")
                plan.escalation_reasons.append(R_INVENTORY)
            else:
                unresolved("availability", R_INVENTORY)
        if A.PRICE in an.intents and fs.availability != "SOLD":
            if fs.price_text:
                if not use_template(A.PRICE):
                    say("price")
                plan.answered.append(A.PRICE)
                plan.facts_used.append("price")
            elif A.NEGOTIATION not in an.intents:
                unresolved("price", R_INVENTORY if ("stale" in problems or "conflict" in problems or "unverified" in problems) else R_MISSING)
        if A.MILEAGE in an.intents:
            if fs.mileage_text:
                if not use_template(A.MILEAGE):
                    say("mileage")
                plan.answered.append(A.MILEAGE)
                plan.facts_used.append("mileage")
            else:
                unresolved("mileage", R_MISSING if "conflict" not in problems else R_INVENTORY)
        if A.FEATURES in an.intents:
            approved_lower = [f.lower() for f in fs.features]
            asked = an.requested_features
            if asked:
                for feat in asked:
                    hit = next((f for f in fs.features if feat in f.lower()), None)
                    if hit:
                        say("feature_yes", feature=hit)
                        plan.facts_used.append("features")
                    else:
                        if "features" not in plan.unresolved:
                            unresolved("features", R_MISSING)
                if all(any(a in f for f in approved_lower) for a in asked):
                    plan.answered.append(A.FEATURES)
            elif fs.features:
                if not use_template(A.FEATURES):
                    say("features")
                plan.answered.append(A.FEATURES)
                plan.facts_used.append("features")
            else:
                unresolved("features", R_MISSING)

    if A.HOURS in an.intents:
        if fs.hours_text:
            if not use_template(A.HOURS):
                say("hours")
            plan.answered.append(A.HOURS)
            plan.facts_used.append("hours")
        else:
            unresolved("hours", R_MISSING)
    if A.LOCATION in an.intents:
        if fs.address:
            if not use_template(A.LOCATION):
                say("location")
            plan.answered.append(A.LOCATION)
            plan.facts_used.append("address")
        else:
            unresolved("location", R_MISSING)

    topic_reasons = [r for r in plan.escalation_reasons if r in TOPIC[lang]]
    if topic_reasons:
        topics = [TOPIC[lang][r] for r in topic_reasons]
        joiner = " and " if lang == "en" else " y "
        parts.append(T["topic_human"][lang].format(topic=joiner.join(topics)))

    if A.CANCEL_APPOINTMENT in an.intents:
        say("appt_change")
    elif A.TEST_DRIVE in an.intents and fs.availability != "SOLD":
        if an.requested_time:
            plan.appointment_request = an.requested_time
            ok = within_hours(fs, an.requested_time)
            plan.appointment_request_in_hours = ok
            when = describe_when(an.requested_time, lang)
            if ok is False:
                say("td_closed")
                plan.appointment_request = None
            elif an.requested_time < now_local:
                say("td_ask_time")
                plan.appointment_request = None
            else:
                say("td_request" if ok else "td_hours_unknown", when=when)
                plan.facts_used.append("requested_time")
                plan.answered.append(A.TEST_DRIVE)
                if not has_contact_method:
                    plan.needs_contact = True
                    say("ask_contact")
        else:
            say("td_ask_time")
            plan.answered.append(A.TEST_DRIVE)
            if not has_contact_method:
                plan.needs_contact = True

    if A.UNKNOWN in an.intents and not faq:
        say("unknown")
    elif A.UNKNOWN in an.intents and faq:
        plan.escalation_reasons.remove(R_UNKNOWN)

    if A.THANKS in an.intents and len(parts) <= 1:
        say("thanks")

    escalating = bool(plan.escalation_reasons)
    if (fs.vehicle is not None and not escalating and A.TEST_DRIVE not in an.intents
            and not already_offered_test_drive and fs.availability == "AVAILABLE"):
        say("td_offer")

    plan.body = " ".join(p for p in parts if p).strip()
    plan.body = re.sub(r"\s+", " ", plan.body)
    plan.confidence -= 0.25 * len(plan.unresolved)
    if A.UNKNOWN in an.intents:
        plan.confidence -= 0.4
    plan.confidence = round(max(0.0, min(1.0, plan.confidence)), 2)

    allowed = fs.allowed_strings() + _engine_strings(lang)
    if plan.appointment_request:
        allowed.append(describe_when(plan.appointment_request, lang))
    plan.validation_errors = validate_output(plan.body, allowed_sources=allowed, language=lang)
    if plan.validation_errors:
        # Should never happen for the deterministic composer; treat as a defect → staff review.
        plan.escalation_reasons.append(R_LOW_CONFIDENCE)
        plan.confidence = 0.0
    return plan


def _engine_strings(lang: str) -> list[str]:
    return [v[lang] for v in T.values()]


def llm_rephrase(llm, plan: ReplyPlan, fs: FactSheet) -> tuple[str | None, float]:
    """Ask the LLM to make the grounded draft sound natural. Returns (validated text or None, cost cents)."""
    if llm is None or plan.no_reply or not plan.body:
        return None, 0.0
    lang_name = "Spanish" if plan.language == "es" else "English"
    system = (
        "You polish customer replies for a used-car dealership. Rewrite the DRAFT so it reads naturally and warmly "
        f"in {lang_name}, in at most 3 short sentences more than the draft. Keep every fact exactly as written "
        "(prices, mileage, names, times). Do not add any new facts, numbers, offers, discounts, financing, warranty, "
        "vehicle history or condition claims, and do not confirm appointments. If unsure, return the draft unchanged. "
        "Return only the reply text."
    )
    facts = "\n".join(f"- {f.key}: {f.value}" for f in fs.facts)
    text, cost = llm.complete(system=system, prompt=f"VERIFIED FACTS:\n{facts}\n\nDRAFT:\n{plan.body}")
    if not text:
        return None, cost
    required = [s for s in (fs.price_text if "price" in plan.facts_used else None,
                            fs.mileage_text if "mileage" in plan.facts_used else None) if s]
    allowed = fs.allowed_strings() + _engine_strings(plan.language) + [plan.body]
    errors = validate_output(text, allowed_sources=allowed, language=plan.language, required_strings=required)
    return (None if errors else text), cost
