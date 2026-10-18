"""Optional daily digest email to the operator (DF_NOTIFY_EMAIL). Off when unset."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..audit import track_integration
from ..config import get_settings
from ..integrations.email import OutgoingEmail, get_email_provider
from ..jobs import handler
from . import reports


def digest_text(db: Session) -> str:
    d = reports.daily(db)

    def rs(x):
        return f"{x['real']}" + (f" (+{x['simulated']} simulated)" if x["simulated"] else "")

    lines = [
        "Daily dealerflow digest (real counts; simulated shown separately)",
        f"New leads: {rs(d['new_leads'])}",
        f"Awaiting approval: {rs(d['awaiting_approval'])}",
        f"Emails actually sent today: {rs(d['sent'])}",
        f"Replies: {rs(d['responses'])}  positive: {rs(d['positive'])}  opt-outs: {rs(d['opt_outs'])}",
        f"Tasks due today: {d['tasks_due_today']}  overdue: {d['overdue']}",
        f"Meetings booked today (provider-confirmed): {rs(d['meetings_booked_today'])}",
        f"Integration errors (24h): {len(d['integration_errors'])}  failed jobs: {len(d['failed_jobs'])}",
        f"\n{get_settings().base_url}/",
    ]
    return "\n".join(lines)


@handler("daily_digest")
def daily_digest_job(db: Session, payload: dict) -> dict:
    s = get_settings()
    if not s.notify_email or not s.sender_email:
        return {"skipped": "DF_NOTIFY_EMAIL or DF_SENDER_EMAIL not set"}
    provider = get_email_provider()
    with track_integration(db, provider.name, "send_digest", simulated=provider.simulated):
        provider.send(OutgoingEmail(to=s.notify_email, subject="dealerflow daily digest", body=digest_text(db),
                                    from_name=s.sender_name or "dealerflow", from_email=s.sender_email,
                                    idempotency_key=payload.get("day")))
    return {"sent_to": s.notify_email, "simulated": provider.simulated}
