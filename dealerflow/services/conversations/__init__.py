"""Phase 3: dealership customer conversations (ported from the Phase 3 ``app/`` package).

Inbound messages (website chat, ADF leads, Marketplace-assisted, Messenger/SMS webhooks) → grounded EN/ES draft
replies → staff review or policy-gated automation → outbound send through the send gate and ledger; escalations,
test-drive appointment requests, staff notifications and the evaluation set. Every external provider is a
sandbox adapter in this build: nothing reaches a real customer.

Importing this package registers its job handlers (see ``dealerflow.handlers``).
"""

from datetime import datetime

from sqlalchemy.orm import Session


def schedule(db: Session, now: datetime | None = None) -> int:
    """Called by the worker every loop: hourly digest check and reminder recovery sweep (idempotent per hour)."""
    from ...db import utcnow
    from ...jobs import enqueue

    now = now or utcnow()
    hour = now.strftime("%Y-%m-%dT%H")
    created = 0
    for kind in ("conversations.daily_digests", "conversations.dispatch_reminders"):
        _, c = enqueue(db, kind, f"{kind}:{hour}", {}, max_attempts=2)
        created += int(c)
    return created
