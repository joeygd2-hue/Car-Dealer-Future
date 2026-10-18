"""Personalized outreach drafts built ONLY from recorded prospect facts.

Template syntax (campaign step subject/body):
  {placeholder}          — filled from facts; if unavailable the draft is not created
  lines starting with "?" — optional; dropped when any placeholder in the line is unavailable
Available placeholders: greeting_name, dealership_name, city, observation, sender_name, company_name
"""

from __future__ import annotations

import secrets
import string

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import Campaign, Enrollment, OutreachMessage, Prospect, ProspectFact

DEFAULT_STEPS = [
    {"day_offset": 0, "subject": "Bilingual listings and faster replies for {dealership_name}",
     "body": ("Hi {greeting_name},\n\n"
              "I work with independent dealerships around {city} on English and Spanish vehicle listings and on "
              "answering shopper messages quickly, with your team approving everything that goes out.\n"
              "?I noticed: {observation}\n\n"
              "Would a 20-minute call next week be useful? If not, no problem at all.\n\n"
              "{sender_name}\n{company_name}")},
    {"day_offset": 4, "subject": "Following up: {dealership_name}",
     "body": ("Hi {greeting_name},\n\nJust following up on my note about bilingual listings and inquiry response for "
              "{dealership_name}. Happy to share examples if helpful.\n\n{sender_name}\n{company_name}")},
    {"day_offset": 10, "subject": "Last note for {dealership_name}",
     "body": ("Hi {greeting_name},\n\nI won't keep following up. If bilingual listings or faster lead response become a "
              "priority, just reply and I'll send times to talk.\n\n{sender_name}\n{company_name}")},
]


def _fields(text: str) -> list[str]:
    return [f for _, f, _, _ in string.Formatter().parse(text) if f]


def active_facts(db: Session, prospect: Prospect) -> list[ProspectFact]:
    now = utcnow()
    return [f for f in db.scalars(select(ProspectFact).where(ProspectFact.prospect_id == prospect.id))
            if f.expires_at is None or f.expires_at > now]


def fill(template: str, values: dict[str, str | None]) -> str | None:
    out = []
    for line in template.split("\n"):
        optional = line.startswith("?")
        body = line[1:] if optional else line
        needed = _fields(body)
        if all(values.get(f) for f in needed):
            out.append(body.format(**{f: values[f] for f in needed}))
        elif not optional:
            return None
    return "\n".join(out)


def compliance_footer(token: str) -> str:
    s = get_settings()
    base = s.base_url.rstrip("/")
    return ("\n\n—\n"
            f"This is a business advertisement from {s.outreach_company_name or '[company name not configured]'}, "
            f"{s.outreach_postal_address or '[postal address not configured]'}.\n"
            f"To unsubscribe, reply \"unsubscribe\" or visit {base}/u/{token}")


def build_draft(db: Session, enrollment: Enrollment) -> OutreachMessage | None:
    campaign = db.get(Campaign, enrollment.campaign_id)
    prospect = db.get(Prospect, enrollment.prospect_id)
    steps = campaign.steps or DEFAULT_STEPS
    if enrollment.current_step >= len(steps):
        return None
    key = f"enr:{enrollment.id}:step:{enrollment.current_step}"
    existing = db.scalar(select(OutreachMessage).where(OutreachMessage.idempotency_key == key))
    if existing:
        return existing
    step = steps[enrollment.current_step]
    facts = active_facts(db, prospect)
    by_key = {f.key: f for f in facts}
    observation = next((f for f in facts if f.is_observation), None)
    s = get_settings()
    values = {
        "greeting_name": (prospect.contact_name.split()[0] if prospect.contact_name else "there"),
        "dealership_name": prospect.name,
        "city": prospect.city,
        "observation": observation.value if observation else None,
        "sender_name": s.outreach_sender_name or "[sender name not configured]",
        "company_name": s.outreach_company_name or "[company name not configured]",
    }
    subject = fill(step["subject"], values)
    body = fill(step["body"], values)
    if not subject or not body or not prospect.email:
        return None
    used = [k for k in ("business_name", "contact_name", "address") if k in by_key]
    if observation and "{observation}" in step["body"]:
        used.append(observation.key)
    used_facts = [by_key[k] if k in by_key else observation for k in used]
    token = secrets.token_urlsafe(16)
    reasons = []
    if campaign.require_message_approval:
        reasons.append("campaign requires per-message approval")
    if not campaign.content_approved_at:
        reasons.append("campaign content not yet approved")
    if any(f and f.confidence != "VERIFIED" for f in used_facts if f.is_observation or f.key == "contact_name"):
        reasons.append("uses unverified prospect facts")
    msg = OutreachMessage(enrollment_id=enrollment.id, prospect_id=prospect.id, campaign_id=campaign.id,
                          step_index=enrollment.current_step, channel=campaign.channel, to_address=prospect.email,
                          subject=f"ADV: {subject}"[:300], body=body + compliance_footer(token),
                          status="PENDING_APPROVAL" if reasons else "APPROVED", approval_reasons=reasons,
                          idempotency_key=key, unsubscribe_token=token,
                          facts_used=[{"key": f.key, "value": f.value, "source": f.source_name, "url": f.source_url,
                                       "confidence": f.confidence} for f in used_facts if f])
    db.add(msg)
    db.flush()
    return msg
