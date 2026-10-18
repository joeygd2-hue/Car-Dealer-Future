"""Drafting, approving and sending outreach email.

Drafts are rendered only from verified data. A template variable with no verified value
blocks the draft instead of being guessed. Every message gets a compliance footer.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from ..audit import audit, track_integration
from ..config import get_settings
from ..db import utcnow
from ..integrations.email import OutgoingEmail, get_email_provider
from ..integrations.errors import CredentialError, IntegrationError, NotConfiguredError, RateLimitedError
from ..jobs import DeferJob, enqueue, handler, on_failure
from ..models import AgentState, Contact, Evidence, InboundMessage, OutreachMessage, Prospect, Template, User
from ..security import role_at_least, sign
from .compliance import email_blockers, suppress
from .crm import add_activity, advance_stage, create_task

ALLOWED_VARS = {
    "dealership_name": "Dealership name",
    "city": "City",
    "contact_first_name": "First name of a verified contact",
    "contact_role": "Role of a verified contact",
    "observation": "A person-verified observation (evidence marked verified)",
    "sender_name": "Your name (DF_SENDER_NAME)",
    "business_name": "Your business name (DF_BUSINESS_NAME)",
}
HUMAN_ONLY_VARS = {"observation"}  # free-text facts: a person must read the message before it goes out

FOOTER = {
    "en": ("\n\n--\n{sender_name}\n{business_name}\n{postal_address}\n\n"
           "This is a commercial message from {business_name}. "
           "If you'd rather not hear from us, unsubscribe here: {unsubscribe_url} or reply \"unsubscribe\"."),
    "es": ("\n\n--\n{sender_name}\n{business_name}\n{postal_address}\n\n"
           "Este es un mensaje comercial de {business_name}. "
           "Si prefiere no recibir más mensajes, cancele su suscripción aquí: {unsubscribe_url} o responda \"baja\"."),
}


class DraftError(ValueError):
    pass


@dataclass
class Rendered:
    subject: str
    body: str
    to_email: str
    contact_id: int | None
    review_flags: list[str]


def template_vars(tpl: Template) -> set[str]:
    names = set()
    for part in (tpl.subject, tpl.body):
        for _, fld, _, _ in string.Formatter().parse(part):
            if fld is not None:
                names.add(fld)
    return names


def validate_template(subject: str, body: str) -> list[str]:
    errs = []
    for part in (subject, body):
        try:
            for _, fld, _, _ in string.Formatter().parse(part):
                if fld is not None and fld not in ALLOWED_VARS:
                    errs.append(f"Unknown variable {{{fld}}}. Allowed: {', '.join(ALLOWED_VARS)}")
        except ValueError as exc:
            errs.append(f"Template syntax error: {exc}")
    return errs


def pick_recipient(p: Prospect) -> tuple[str | None, Contact | None]:
    verified = [c for c in p.contacts if c.verified and c.email]
    primary = next((c for c in verified if c.is_primary), verified[0] if verified else None)
    if primary:
        return primary.email, primary
    return p.email, None


def unsubscribe_url(email: str, prospect_id: int) -> str:
    return f"{get_settings().base_url}/u/{sign({'e': email, 'p': prospect_id}, 'unsubscribe')}"


def render(db: Session, p: Prospect, tpl: Template, *, observation_evidence_id: int | None = None) -> Rendered:
    s = get_settings()
    to, contact = pick_recipient(p)
    if not to:
        raise DraftError("No public business email or verified contact email on record")
    values: dict[str, str | None] = {
        "dealership_name": p.name.replace("(SIMULATED)", "").strip(),
        "city": p.city,
        "contact_first_name": (contact.name.split()[0] if contact and contact.name else None),
        "contact_role": contact.role if contact and contact.role else None,
        "sender_name": s.sender_name or None,
        "business_name": s.business_name or None,
        "observation": None,
    }
    if observation_evidence_id:
        ev = db.get(Evidence, observation_evidence_id)
        if not ev or ev.prospect_id != p.id:
            raise DraftError("Observation evidence not found for this prospect")
        if not ev.verified:
            raise DraftError("Observation has not been verified by a person; verify it before using it in a message")
        values["observation"] = ev.note or ev.value
    needed = template_vars(tpl)
    missing = [v for v in needed if not values.get(v)]
    if missing:
        raise DraftError("Missing verified values for: " + ", ".join(f"{{{m}}} ({ALLOWED_VARS.get(m, m)})" for m in missing))
    subject = tpl.subject.format(**values)
    body = tpl.body.format(**values)
    flags = [f"uses {{{v}}}" for v in needed & HUMAN_ONLY_VARS]
    return Rendered(subject, body, to, contact.id if contact else None, flags)


def with_footer(body: str, language: str, to_email: str, prospect_id: int) -> str:
    s = get_settings()
    return body.rstrip() + FOOTER.get(language, FOOTER["en"]).format(
        sender_name=s.sender_name, business_name=s.business_name, postal_address=s.business_postal_address,
        unsubscribe_url=unsubscribe_url(to_email, prospect_id))


def create_draft(db: Session, p: Prospect, tpl: Template, *, actor: User | None, origin: str = "human",
                 campaign_id: int | None = None, step: int | None = None,
                 observation_evidence_id: int | None = None) -> OutreachMessage:
    if p.do_not_contact:
        raise DraftError("Prospect is marked do-not-contact")
    r = render(db, p, tpl, observation_evidence_id=observation_evidence_id)
    simulated = p.simulated or get_email_provider_name() == "mock"
    msg = OutreachMessage(
        prospect_id=p.id, contact_id=r.contact_id, template_id=tpl.id, to_email=r.to_email, subject=r.subject,
        body=with_footer(r.body, tpl.language, r.to_email, p.id), language=tpl.language, status="draft",
        created_by_id=actor.id if actor else None, simulated=simulated, origin=origin, campaign_id=campaign_id,
        enrollment_step=step, review_flags=r.review_flags or None,
    )
    db.add(msg)
    db.flush()
    add_activity(db, p, "email_draft", f"Draft created: {msg.subject}", actor_id=actor.id if actor else None,
                 ref_type="outreach", ref_id=msg.id, data={"origin": origin, "template": tpl.name})
    audit(db, "outreach.draft", actor_id=actor.id if actor else None, entity_type="outreach", entity_id=msg.id,
          data={"origin": origin, "template_id": tpl.id})
    return msg


def create_reply_draft(db: Session, inbound: InboundMessage, body: str, *, actor: User | None,
                       origin: str = "human", flags: list[str] | None = None) -> OutreachMessage:
    p = inbound.prospect
    if p is None:
        raise DraftError("Reply is not associated with a prospect")
    subj = inbound.subject if inbound.subject.lower().startswith("re:") else f"Re: {inbound.subject}"
    lang = p.preferred_language or "en"
    parent = db.get(OutreachMessage, inbound.outreach_message_id) if inbound.outreach_message_id else None
    msg = OutreachMessage(
        prospect_id=p.id, to_email=inbound.from_email, subject=subj[:255],
        body=with_footer(body, lang, inbound.from_email, p.id), language=lang, status="draft",
        created_by_id=actor.id if actor else None, simulated=inbound.simulated or get_email_provider_name() == "mock",
        origin=origin, in_reply_to_inbound_id=inbound.id, thread_id=inbound.thread_id or (parent.thread_id if parent else None),
        review_flags=flags or None,
    )
    db.add(msg)
    db.flush()
    add_activity(db, p, "email_draft", f"Reply drafted ({origin}): {subj}", actor_id=actor.id if actor else None,
                 ref_type="outreach", ref_id=msg.id)
    return msg


def get_email_provider_name() -> str:
    return get_settings().email_provider


class ApprovalError(ValueError):
    pass


def approve(db: Session, msg: OutreachMessage, *, actor: User | None, kind: str = "human") -> OutreachMessage:
    """Approve and queue. ``kind='campaign_auto'`` is only used by the agent under a campaign authorization."""
    if msg.status != "draft":
        raise ApprovalError(f"Message is {msg.status}, not draft")
    if kind == "human":
        if actor is None or not role_at_least(actor.role, "manager"):
            raise ApprovalError("Only a manager or admin can approve outbound messages")
    blockers = email_blockers(db, msg)
    if blockers:
        raise ApprovalError("; ".join(blockers))
    now = utcnow()
    msg.status, msg.approved_at, msg.approval_kind = "queued", now, kind
    msg.approved_by_id = actor.id if actor else None
    msg.queued_at = now
    p = msg.prospect
    if not msg.in_reply_to_inbound_id:
        advance_stage(db, p, "outreach_approved", reason=f"Message #{msg.id} approved ({kind})")
    add_activity(db, p, "email_approved", f"Approved ({kind}): {msg.subject}", actor_id=actor.id if actor else None,
                 ref_type="outreach", ref_id=msg.id)
    audit(db, "outreach.approve", actor_id=actor.id if actor else None, entity_type="outreach", entity_id=msg.id,
          data={"kind": kind})
    enqueue(db, "send_email", f"send_email:{msg.id}", {"message_id": msg.id}, max_attempts=4)
    return msg


def reject(db: Session, msg: OutreachMessage, *, actor: User, reason: str) -> None:
    if msg.status not in ("draft", "queued", "approved"):
        raise ApprovalError(f"Cannot cancel a message that is {msg.status}")
    msg.status, msg.error = "cancelled", f"cancelled by {actor.email}: {reason}"
    add_activity(db, msg.prospect, "email_cancelled", f"Message cancelled: {msg.subject}", body=reason,
                 actor_id=actor.id, ref_type="outreach", ref_id=msg.id)
    audit(db, "outreach.cancel", actor_id=actor.id, entity_type="outreach", entity_id=msg.id, data={"reason": reason})


def live_sending_allowed(db: Session) -> tuple[bool, str]:
    s = get_settings()
    if not s.outreach_sending_enabled:
        return False, "DF_OUTREACH_SENDING_ENABLED is false"
    st = db.get(AgentState, 1)
    if not st or not st.live_authorized:
        return False, "Live operation has not been authorized by an admin (Agent page)"
    if st.status == "emergency_stopped":
        return False, "Emergency stop is active"
    return True, ""


@handler("send_email")
def send_email_job(db: Session, payload: dict) -> dict:
    msg = db.get(OutreachMessage, payload["message_id"])
    if msg is None:
        return {"skipped": "message deleted"}
    if msg.status in ("sent", "delivered", "replied", "bounced", "cancelled", "failed"):
        return {"skipped": f"already {msg.status}"}  # idempotent re-run
    if msg.status == "sending":
        # A previous attempt reached the provider and its outcome is unknown. Never resend blindly.
        msg.status, msg.error = "failed", "Outcome unknown after an interrupted send; check the mailbox before retrying"
        add_activity(db, msg.prospect, "email_failed", f"Send outcome unknown: {msg.subject}", ref_type="outreach", ref_id=msg.id)
        return {"failed": "ambiguous previous attempt"}
    provider = get_email_provider()
    st = db.get(AgentState, 1)
    if st and st.status == "emergency_stopped":
        raise DeferJob(utcnow() + timedelta(hours=1), "emergency stop active")
    hold = _campaign_hold(db, msg, st)
    if hold:
        raise DeferJob(utcnow() + timedelta(minutes=30), hold)
    if not provider.simulated:
        ok, why = live_sending_allowed(db)
        if not ok:
            raise DeferJob(utcnow() + timedelta(hours=1), why)
    blockers = email_blockers(db, msg, at_send=True, provider_simulated=provider.simulated)
    if blockers:
        if any(b.startswith("Daily send cap") for b in blockers) and len(blockers) == 1:
            tomorrow = (utcnow() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
            raise DeferJob(tomorrow, "daily cap reached")
        msg.status, msg.error = "cancelled", "Blocked at send time: " + "; ".join(blockers)
        add_activity(db, msg.prospect, "email_blocked", f"Send blocked: {msg.subject}", body=msg.error,
                     ref_type="outreach", ref_id=msg.id)
        audit(db, "outreach.blocked", entity_type="outreach", entity_id=msg.id, data={"blockers": blockers})
        return {"blocked": blockers}
    s = get_settings()
    in_reply_to = None
    if msg.in_reply_to_inbound_id:
        inbound = db.get(InboundMessage, msg.in_reply_to_inbound_id)
        in_reply_to = inbound.rfc_message_id if inbound else None
    out = OutgoingEmail(to=msg.to_email, subject=msg.subject, body=msg.body, from_name=s.sender_name,
                        from_email=s.sender_email, unsubscribe_url=unsubscribe_url(msg.to_email, msg.prospect_id),
                        unsubscribe_mailto=s.sender_email, in_reply_to=in_reply_to, thread_id=msg.thread_id,
                        idempotency_key=f"df{msg.id}")
    msg.status = "sending"
    db.commit()  # checkpoint before the external call
    try:
        with track_integration(db, provider.name, "send_email", simulated=provider.simulated) as ctx:
            res = provider.send(out)
            ctx["detail"] = f"message #{msg.id} -> {res.provider_message_id}"
    except (CredentialError, NotConfiguredError, RateLimitedError) as exc:
        msg.status, msg.error = "queued", str(exc)  # provider refused before sending
        db.commit()
        raise
    except IntegrationError as exc:
        msg.status, msg.error = "failed", str(exc)
        add_activity(db, msg.prospect, "email_failed", f"Send failed: {msg.subject}", body=str(exc),
                     ref_type="outreach", ref_id=msg.id)
        db.commit()
        return {"failed": str(exc)}
    now = utcnow()
    msg.status, msg.sent_at, msg.provider = "sent", now, provider.name
    msg.provider_message_id, msg.rfc_message_id = res.provider_message_id, res.rfc_message_id
    msg.thread_id = res.thread_id or msg.thread_id
    msg.simulated = msg.simulated or res.simulated
    msg.error = None
    p = msg.prospect
    tag = " [SIMULATED]" if res.simulated else ""
    add_activity(db, p, "email_out", f"Email sent{tag}: {msg.subject}", body=msg.body, ref_type="outreach",
                 ref_id=msg.id, data={"to": msg.to_email, "provider": provider.name})
    # A simulated send to a real prospect must not make them look contacted in the pipeline.
    pipeline_effect = not (res.simulated and not p.simulated)
    if not msg.in_reply_to_inbound_id and pipeline_effect:
        advance_stage(db, p, "contacted", reason=f"Message #{msg.id} sent{tag}")
        create_task(db, f"Follow up with {p.name} if no reply", now + timedelta(days=4), prospect=p,
                    kind="follow_up", dedupe_key=f"followup:sent:{msg.id}")
    audit(db, "outreach.sent", entity_type="outreach", entity_id=msg.id,
          data={"provider": provider.name, "simulated": res.simulated, "provider_message_id": res.provider_message_id})
    return {"sent": msg.id, "simulated": res.simulated}


def _campaign_hold(db: Session, msg: OutreachMessage, st: AgentState | None) -> str | None:
    """Reasons to hold an already-queued message (it stays queued and is retried later)."""
    from ..models import Campaign
    from .agent import config as agent_config, in_operating_window

    if msg.campaign_id:
        camp = db.get(Campaign, msg.campaign_id)
        if camp is not None and camp.status != "active":
            return f"campaign '{camp.name}' is {camp.status}"
    if msg.approval_kind == "campaign_auto":
        if st is None or st.status != "running":
            return f"agent is {st.status if st else 'stopped'}; agent-approved messages wait"
        if not in_operating_window(agent_config(db), utcnow()):
            return "outside the agent's operating hours"
    return None


@on_failure("send_email")
def _send_failed(db: Session, payload: dict, error: str) -> None:
    msg = db.get(OutreachMessage, payload["message_id"])
    if msg and msg.status in ("queued", "sending"):
        msg.status, msg.error = "failed", error
        add_activity(db, msg.prospect, "email_failed", f"Send failed: {msg.subject}", body=error,
                     ref_type="outreach", ref_id=msg.id)


def handle_unsubscribe(db: Session, token_value: dict, *, source: str) -> str:
    email = token_value.get("e")
    if not email:
        raise ValueError("bad token")
    suppress(db, email, "unsubscribe", source=source)
    pid = token_value.get("p")
    if pid and db.get(Prospect, pid):
        add_activity(db, pid, "opt_out", f"Unsubscribed via link: {email}")
    return email


