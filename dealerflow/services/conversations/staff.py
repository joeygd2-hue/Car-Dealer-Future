"""Staff actions on conversations, shared by the dealer pages and the JSON API (ported from Phase 3 ``web/portal.py``).

Every function takes a ``ClientContext`` (tenant-scoped session + server-side role check already done by
``dealer_ctx``) and looks objects up by id *within that client*; another dealership's ids raise ``NotFound``.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select

from ...db import utcnow
from ...models import (
    Appointment,
    Conversation,
    ConversationEscalation,
    ConversationMessage,
    DealerFAQ,
    ResponseDraft,
    ResponseModeAuthorization,
    ResponseTemplate,
    User,
)
from ...models_conversations import MARKETPLACE_ASSISTED, MODE_DRAFT_ONLY, MODES, SENDABLE_CHANNELS
from ...models_dealer import Vehicle
from ...tenancy import ClientContext
from ..dealer_errors import BadRequest, Forbidden, NotFound
from . import appointments as appt_svc
from . import escalation, evaluation
from .common import log
from .inbox import ingest_inbound
from .outbound import current_fact_snapshot, queue_outbound, reconcile_uncertain, record_manual_external
from .policy import CONTROLLABLE_INTENTS, settings_row, valid_mode_authorization
from .summaries import build_summary


def get_owned(ctx: ClientContext, model, obj_id: int | None):
    obj = ctx.db.get(model, obj_id) if obj_id is not None else None
    if obj is None or obj.client_id != ctx.client_id:
        raise NotFound(f"{model.__name__} not found")
    return obj


def _require(ctx: ClientContext, permission: str) -> None:
    if not ctx.can(permission):
        raise Forbidden(f"Your role cannot {permission} in this dealership workspace")


def staff_message_warnings(ctx: ClientContext, conv: Conversation, body: str, draft: ResponseDraft | None) -> list[str]:
    """Run the same safety checker on the exact text staff are about to send."""
    from .responder.engine import T
    from .responder.safety import validate_output

    _, fs = current_fact_snapshot(ctx.db, conv)
    allowed = fs.allowed_strings() + [x[conv.preferred_language] for x in T.values()]
    if draft:
        allowed.append(draft.body)
    # Staff may legitimately write in another language or confirm a time they booked themselves.
    return [v for v in validate_output(body, allowed_sources=allowed, language=conv.preferred_language,
                                       appointment_confirmed=True) if not v.startswith("language mismatch")]


def send_reply(ctx: ClientContext, conv: Conversation, body: str, *, draft_id: int | None = None,
               acknowledge_warnings: bool = False, manual_external: bool = False) -> dict:
    """Staff review → (edit) → approve. Returns {"status": ..., "warnings": [...]}; nothing is sent while warnings
    are unacknowledged. A website-chat reply is queued for the send job; other channels are recorded as sent by staff."""
    _require(ctx, "converse")
    body = body.strip()
    if not body:
        raise BadRequest("Message is empty")
    if len(body) > 5000:
        raise BadRequest("Message is too long (5,000 characters at most)")
    draft = get_owned(ctx, ResponseDraft, draft_id) if draft_id else None
    if draft and draft.conversation_id != conv.id:
        raise NotFound("ResponseDraft not found")
    if draft and draft.status != "pending_review":
        raise BadRequest(f"This draft is already {draft.status}")
    if conv.opted_out:
        raise BadRequest("The customer opted out. Messages cannot be sent from the platform.")
    warnings = staff_message_warnings(ctx, conv, body, draft)
    if warnings and not acknowledge_warnings:
        return {"status": "needs_acknowledgement", "warnings": warnings}
    if warnings:
        log(ctx.db, "conversation.staff_override", client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id,
            actor_id=ctx.actor_id, actor_type="user", warnings=warnings)
    if draft:
        draft.reviewed_by_id, draft.reviewed_at = ctx.actor_id, utcnow()
        if draft.body != body:
            log(ctx.db, "conversation.draft_edited", client_id=ctx.client_id, entity_type="response_draft", entity_id=draft.id,
                actor_id=ctx.actor_id, actor_type="user")
    if conv.channel not in SENDABLE_CHANNELS or manual_external:
        record_manual_external(ctx.db, conv, body, user_id=ctx.actor_id, draft=draft)
        build_summary(ctx.db, conv)
        return {"status": "manual_external", "warnings": warnings}
    # Double-submit protection: one message per draft; without a draft, one per identical text per minute.
    key = (f"staff:d{draft.id}" if draft else
           f"staff:c{conv.id}:{hashlib.sha256(body.encode()).hexdigest()[:16]}:{utcnow():%Y%m%d%H%M}")
    msg = queue_outbound(ctx.db, conv, body, sender_type="staff", user_id=ctx.actor_id, draft=draft, idempotency_key=key)
    if draft:
        draft.status = "sent"
    log(ctx.db, "conversation.reply_approved", client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id,
        actor_id=ctx.actor_id, actor_type="user", message_id=msg.id, draft_id=draft.id if draft else None,
        edited=bool(draft and draft.body != body))
    if conv.assigned_user_id is None and ctx.user is not None:
        conv.assigned_user_id = ctx.user.id
    build_summary(ctx.db, conv)
    return {"status": "queued", "message_id": msg.id, "warnings": warnings}


def reject_draft(ctx: ClientContext, conv: Conversation, draft_id: int) -> None:
    _require(ctx, "converse")
    draft = get_owned(ctx, ResponseDraft, draft_id)
    if draft.conversation_id != conv.id or draft.status != "pending_review":
        raise NotFound("ResponseDraft not found")
    draft.status, draft.reviewed_by_id, draft.reviewed_at = "rejected", ctx.actor_id, utcnow()
    log(ctx.db, "conversation.draft_rejected", client_id=ctx.client_id, entity_type="response_draft", entity_id=draft.id,
        actor_id=ctx.actor_id, actor_type="user")


def members(ctx: ClientContext) -> list[User]:
    return list(ctx.db.scalars(select(User).where(User.client_id == ctx.client_id, User.active.is_(True))))


def assign(ctx: ClientContext, conv: Conversation, user_id: int | None) -> None:
    _require(ctx, "converse")
    if user_id is not None:
        u = ctx.db.get(User, user_id)
        if u is None or not u.active or u.client_id not in (None, ctx.client_id):  # never another dealership's user
            raise BadRequest("That user cannot be assigned to this dealership's conversations")
    conv.assigned_user_id = user_id
    log(ctx.db, "conversation.assigned", client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id,
        actor_id=ctx.actor_id, actor_type="user", assignee=user_id)


def takeover(ctx: ClientContext, conv: Conversation, on: bool) -> None:
    _require(ctx, "converse")
    conv.human_takeover = on
    conv.takeover_by_id, conv.takeover_at = (ctx.actor_id, utcnow()) if on else (None, None)
    if not on:
        conv.human_requested = False
    log(ctx.db, "conversation.takeover_on" if on else "conversation.takeover_off", client_id=ctx.client_id,
        entity_type="conversation", entity_id=conv.id, actor_id=ctx.actor_id, actor_type="user")


def link_vehicle(ctx: ClientContext, conv: Conversation, vehicle_id: int | None) -> None:
    _require(ctx, "converse")
    if vehicle_id:
        get_owned(ctx, Vehicle, vehicle_id)
    conv.vehicle_id = vehicle_id
    build_summary(ctx.db, conv)


def close(ctx: ClientContext, conv: Conversation) -> None:
    _require(ctx, "converse")
    conv.status = "closed"
    log(ctx.db, "conversation.closed", client_id=ctx.client_id, entity_type="conversation", entity_id=conv.id,
        actor_id=ctx.actor_id, actor_type="user")


def resolve_escalation(ctx: ClientContext, escalation_id: int) -> ConversationEscalation:
    _require(ctx, "converse")
    esc = get_owned(ctx, ConversationEscalation, escalation_id)
    escalation.resolve(ctx.db, esc, ctx.user)
    build_summary(ctx.db, ctx.db.get(Conversation, esc.conversation_id))
    return esc


def reconcile(ctx: ClientContext, message_id: int, delivered: bool) -> ConversationMessage:
    _require(ctx, "converse")
    msg = get_owned(ctx, ConversationMessage, message_id)
    try:
        reconcile_uncertain(ctx.db, msg, delivered=delivered, user_id=ctx.actor_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return msg


def marketplace_intake(ctx: ClientContext, *, thread_label: str, text: str, vehicle_id: int | None, buyer_name: str):
    """Human-assisted Marketplace workflow: staff paste a buyer message they read in Facebook themselves."""
    _require(ctx, "converse")
    if not thread_label.strip() or not text.strip():
        raise BadRequest("Thread label and message are required")
    if vehicle_id:
        get_owned(ctx, Vehicle, vehicle_id)
    thread = "mp:" + hashlib.sha256(thread_label.strip().lower().encode()).hexdigest()[:24]
    pid = hashlib.sha256(f"{thread}:{text.strip()}".encode()).hexdigest()[:40]  # duplicate paste → same id
    res = ingest_inbound(ctx.db, client=ctx.client, channel=MARKETPLACE_ASSISTED, external_conversation_id=thread,
                         text=text.strip(), provider_message_id=pid, contact_hints={"name": buyer_name.strip() or None},
                         vehicle_id=vehicle_id, simulated=ctx.client.simulated)
    if res.conversation.assigned_user_id is None and ctx.user is not None:
        res.conversation.assigned_user_id = ctx.user.id
    log(ctx.db, "conversation.marketplace_intake", client_id=ctx.client_id, entity_type="conversation",
        entity_id=res.conversation.id, actor_id=ctx.actor_id, actor_type="user", duplicate=not res.created)
    return res


def appointment_action(ctx: ClientContext, appointment_id: int, action: str, *, start_utc=None, note: str = "") -> Appointment:
    _require(ctx, "converse")
    appt = get_owned(ctx, Appointment, appointment_id)
    try:
        if action == "confirm":
            appt_svc.confirm(ctx.db, appt, user=ctx.user, start_utc=start_utc)
        elif action == "decline":
            appt_svc.decline(ctx.db, appt, user=ctx.user, note=note or None)
        elif action == "cancel":
            appt_svc.cancel(ctx.db, appt, user=ctx.user, reason=note or None)
        elif action in ("completed", "no_show"):
            appt_svc.complete(ctx.db, appt, user=ctx.user, outcome=action)
        else:
            raise BadRequest("Unknown appointment action")
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return appt


# --- dealership configuration (manage permission) ---------------------------------------------------------------

def save_business_info(ctx: ClientContext, *, phone: str, address_line: str, city: str, state: str, postal_code: str,
                       hours: dict[str, list[str] | None], approve: bool) -> None:
    _require(ctx, "manage")
    c = ctx.client
    c.phone, c.address_line, c.city = phone.strip() or None, address_line.strip() or None, city.strip() or None
    c.state, c.postal_code = (state.strip().upper()[:2] or None), (postal_code.strip()[:10] or None)
    c.business_hours = hours
    # Any change invalidates a previous approval; replies use the details only after someone approves them again.
    c.business_info_approved_at = utcnow() if approve else None
    c.business_info_approved_by_id = ctx.actor_id if approve else None
    log(ctx.db, "conversation.business_info_saved", client_id=ctx.client_id, entity_type="client", entity_id=ctx.client_id,
        actor_id=ctx.actor_id, actor_type="user", approved=approve)


def ensure_public_keys(ctx: ClientContext, rotate: bool = False) -> None:
    _require(ctx, "manage")
    c = ctx.client
    if rotate or not c.public_chat_key:
        c.public_chat_key = secrets.token_urlsafe(18)
    if rotate or not c.adf_intake_key:
        c.adf_intake_key = secrets.token_urlsafe(24)
    log(ctx.db, "conversation.intake_keys_rotated" if rotate else "conversation.intake_keys_created", client_id=ctx.client_id,
        entity_type="client", entity_id=ctx.client_id, actor_id=ctx.actor_id, actor_type="user")


def add_faq(ctx: ClientContext, *, language: str, question: str, answer: str, keywords: str, auto_send: bool) -> DealerFAQ:
    _require(ctx, "manage")
    if language not in ("en", "es") or not question.strip() or not answer.strip():
        raise BadRequest("Language, question and answer are required")
    faq = DealerFAQ(client_id=ctx.client_id, language=language, question=question.strip()[:1000], answer=answer.strip()[:3000],
                    keywords=[k.strip() for k in keywords.split(",") if k.strip()][:20], auto_send_allowed=auto_send,
                    approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(faq)
    ctx.db.flush()
    log(ctx.db, "conversation.faq_approved", client_id=ctx.client_id, entity_type="dealer_faq", entity_id=faq.id,
        actor_id=ctx.actor_id, actor_type="user", auto_send=auto_send)
    return faq


def deactivate_faq(ctx: ClientContext, faq_id: int) -> None:
    _require(ctx, "manage")
    get_owned(ctx, DealerFAQ, faq_id).active = False


TEMPLATE_INTENTS = ("greeting", "availability", "price", "mileage", "features", "hours", "location")


def save_template(ctx: ClientContext, *, intent: str, language: str, body: str, auto_send: bool) -> ResponseTemplate:
    _require(ctx, "manage")
    if intent not in TEMPLATE_INTENTS or language not in ("en", "es") or not body.strip():
        raise BadRequest("Choose an intent and language, and write the template")
    for old in ctx.db.scalars(select(ResponseTemplate).where(ResponseTemplate.client_id == ctx.client_id,
                                                             ResponseTemplate.intent == intent, ResponseTemplate.language == language,
                                                             ResponseTemplate.active.is_(True))):
        old.active = False
    tpl = ResponseTemplate(client_id=ctx.client_id, intent=intent, language=language, body=body.strip()[:2000],
                           auto_send_allowed=auto_send, approved_by_id=ctx.actor_id, approved_at=utcnow())
    ctx.db.add(tpl)
    ctx.db.flush()
    log(ctx.db, "conversation.template_approved", client_id=ctx.client_id, entity_type="response_template", entity_id=tpl.id,
        actor_id=ctx.actor_id, actor_type="user", intent=intent, auto_send=auto_send)
    return tpl


def set_pause(ctx: ClientContext, paused: bool, reason: str) -> None:
    _require(ctx, "manage")
    s = settings_row(ctx.db, ctx.client_id)
    s.paused, s.paused_reason = paused, (reason.strip() or None) if paused else None
    s.paused_by_id, s.paused_at = (ctx.actor_id, utcnow()) if paused else (None, None)
    log(ctx.db, "conversation.automation_paused" if paused else "conversation.automation_resumed", client_id=ctx.client_id,
        entity_type="client", entity_id=ctx.client_id, actor_id=ctx.actor_id, actor_type="user")


def set_limits(ctx: ClientContext, **values) -> None:
    _require(ctx, "manage")
    s = settings_row(ctx.db, ctx.client_id)
    bounds = {"max_auto_replies_per_conversation_per_hour": (0, 20), "max_auto_replies_per_dealership_per_hour": (0, 500),
              "min_seconds_between_auto_replies": (0, 3600), "stale_inventory_hours": (1, 168)}
    for k, (lo, hi) in bounds.items():
        if k in values:
            setattr(s, k, max(lo, min(hi, int(values[k]))))
    if "confidence_threshold" in values:
        s.confidence_threshold = max(0.5, min(1.0, float(values["confidence_threshold"])))
    if "auto_reply_outside_business_hours" in values:
        s.auto_reply_outside_business_hours = bool(values["auto_reply_outside_business_hours"])
    log(ctx.db, "conversation.automation_limits_saved", client_id=ctx.client_id, entity_type="client", entity_id=ctx.client_id,
        actor_id=ctx.actor_id, actor_type="user")


def run_evaluation(ctx: ClientContext):
    _require(ctx, "manage")
    return evaluation.run_and_record(ctx.db, user=ctx.user, client_id=ctx.client_id)


def set_mode(ctx: ClientContext, mode: str, *, statement: str, evaluation_run_id: int | None, controlled_intents: list[str]) -> None:
    """Modes above draft-only need the dealership's written authorization tied to a passing evaluation (Phase 3 rule).
    Automatic sending additionally needs the Phase 4 onboarding gate, which agency staff enable separately."""
    from ...models import ResponseEvaluationRun
    from .responder.engine import ENGINE_VERSION

    _require(ctx, "manage")
    if mode not in MODES:
        raise BadRequest("Unknown mode")
    s = settings_row(ctx.db, ctx.client_id)
    if mode == MODE_DRAFT_ONLY:
        for a in ctx.db.scalars(select(ResponseModeAuthorization).where(ResponseModeAuthorization.client_id == ctx.client_id,
                                                                        ResponseModeAuthorization.revoked_at.is_(None))):
            a.revoked_at, a.revoked_by_id = utcnow(), ctx.actor_id
        s.mode = MODE_DRAFT_ONLY
    else:
        run = ctx.db.get(ResponseEvaluationRun, evaluation_run_id) if evaluation_run_id else None
        if run is None or run.client_id not in (None, ctx.client_id) or not run.passed or run.engine_version != ENGINE_VERSION:
            raise BadRequest("Run the evaluation first: a passing evaluation of the current engine is required")
        if len(statement.strip()) < 20:
            raise BadRequest("Record the dealership's written authorization (who approved it, when and how)")
        ctx.db.add(ResponseModeAuthorization(client_id=ctx.client_id, mode=mode, statement=statement.strip()[:2000],
                                             authorized_by_id=ctx.actor_id, evaluation_run_id=run.id))
        s.mode = mode
        s.controlled_intents = sorted(set(controlled_intents) & CONTROLLABLE_INTENTS)
        ctx.db.flush()
        if valid_mode_authorization(ctx.db, ctx.client_id, mode) is None:
            raise BadRequest("The authorization could not be validated")
    log(ctx.db, "conversation.mode_set", client_id=ctx.client_id, entity_type="client", entity_id=ctx.client_id,
        actor_id=ctx.actor_id, actor_type="user", mode=mode)
