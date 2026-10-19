"""Shared fixtures for the Phase 3 (customer conversations) tests: two dealerships with approved business info,
hours, a verified Civic, a sandbox website-chat connection, a dealer admin and a dealer reviewer each, plus agency
staff. Fixture data only; every provider is a sandbox."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dealerflow.db import session_scope, utcnow
from dealerflow.models import Client, IntegrationConnection, User
from dealerflow.models_conversations import NotificationPreference, ResponseAutomationSettings
from dealerflow.models_dealer import InventorySource, Vehicle
from dealerflow.security import hash_password

PASSWORD = "correct horse battery"
HOURS = {str(d): (["09:00", "19:00"] if d < 6 else None) for d in range(7)}


def _ids(obj) -> dict:
    return {"id": obj.id}


class Ref:
    """A detached id holder, so tests can use ``world["a"]["d"].id`` like the Phase 3 originals."""

    def __init__(self, id: int, **extra):
        self.id = id
        self.__dict__.update(extra)


def build_world(db) -> dict:
    out: dict = {}
    pw = hash_password(PASSWORD)
    for key, name in (("a", "Alpha Auto"), ("b", "Bravo Motors")):
        c = Client(name=name, status="active", phone="(281) 555-0100", address_line=f"{key.upper()}1 Test Rd",
                   city="Sugar Land", state="TX", postal_code="77478", timezone="America/Chicago",
                   business_hours=dict(HOURS), business_info_approved_at=utcnow(), contact_email=f"owner@{key}.example")
        db.add(c)
        db.flush()
        db.add(ResponseAutomationSettings(client_id=c.id, auto_reply_outside_business_hours=True,
                                          min_seconds_between_auto_replies=5))
        db.add(IntegrationConnection(client_id=c.id, kind="customer_messaging", provider="website_chat",
                                     external_account_id=f"web:{c.id}", status="sandbox", config={}))
        admin = User(email=f"admin@{key}.example", name=f"{name} Admin", role="client", client_id=c.id,
                     client_role="dealer_admin", password_hash=pw)
        staff = User(email=f"staff@{key}.example", name=f"{name} Staff", role="client", client_id=c.id,
                     client_role="dealer_reviewer", password_hash=pw)
        db.add_all([admin, staff])
        db.flush()
        db.add(NotificationPreference(client_id=c.id, escalation_user_ids=[admin.id]))
        src = InventorySource(client_id=c.id, name="Fixture feed", source_type="csv_upload", full_snapshot=False)
        db.add(src)
        db.flush()
        verified = utcnow() - timedelta(hours=1)
        civic = Vehicle(client_id=c.id, source_id=src.id, external_id=f"{key.upper()}100", stock_number=f"{key.upper()}100",
                        vin=f"2HGFC2F70KH50000{1 if key == 'a' else 2}", year=2019, make="Honda", model="Civic", trim="EX",
                        price_cents=1899500, mileage=45210, inventory_status="available", verified_at=verified,
                        last_seen_at=verified, features=["Backup camera", "Bluetooth", "Sunroof"],
                        content_fingerprint="f" * 64, photo_fingerprint="p" * 64)
        db.add(civic)
        db.flush()
        out[key] = {"d": Ref(c.id), "admin": Ref(admin.id, email=admin.email), "staff": Ref(staff.id, email=staff.email),
                    "civic": Ref(civic.id), "source": Ref(src.id)}
    platform = User(email="ops@platform.example", name="Operator", role="admin", password_hash=pw)
    rep = User(email="rep@platform.example", name="Rep", role="rep", password_hash=pw)
    db.add_all([platform, rep])
    db.commit()
    out["platform"] = Ref(platform.id, email=platform.email)
    out["rep"] = Ref(rep.id, email=rep.email)
    return out


def chat(dealership, visitor: str, text: str, msg_id: str | None = None, channel: str = "WEBSITE_CHAT"):
    """Ingest a website-chat message directly (bypassing HTTP). Returns (message_id, conversation_id, created)."""
    from dealerflow.services.conversations.inbox import ingest_inbound

    with session_scope() as s:
        c = s.get(Client, dealership.id)
        res = ingest_inbound(s, client=c, channel=channel, external_conversation_id=f"web:{visitor}", text=text,
                             provider_message_id=msg_id or f"{visitor}:{abs(hash(text))}")
        return res.message.id, res.conversation.id, res.created


def run_worker(rounds: int = 10) -> dict:
    """Run due jobs (future-dated backoff jobs are made due first)."""
    from tests.dealer_helpers import drain

    return drain(rounds)


def run_due(rounds: int = 10) -> dict:
    """Run only jobs that are already due (keeps future reminders/backoff in the future)."""
    from dealerflow import handlers  # noqa: F401
    from dealerflow.jobs import run_pending

    total: dict[str, int] = {}
    for _ in range(rounds):
        counts = run_pending()
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
        if not counts:
            break
    return total


def user(db, email: str) -> User:
    return db.scalar(select(User).where(User.email == email))
