"""Seed a SANDBOX demo: platform admin, one synthetic dealership with staff, inventory, FAQs and an outreach campaign.

Usage:  python -m scripts.seed_demo            (prints the demo logins)
All data is synthetic. Nothing is sent anywhere while LIVE_INTEGRATIONS_ENABLED=false.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal, utcnow
from app.models import (
    AutomationSettings,
    BusinessHours,
    Campaign,
    Dealership,
    IntegrationConnection,
    ListingDraft,
    Membership,
    OutreachFAQ,
    User,
    Vehicle,
)
from app.security.passwords import hash_password
from app.services.outreach.drafting import DEFAULT_STEPS

PASSWORD = "demo-password-123"


def main() -> None:
    from sqlalchemy import inspect

    from app.db import get_engine

    if "alembic_version" not in inspect(get_engine()).get_table_names():
        # Never create an unversioned schema: later `alembic upgrade` would fail on it.
        raise SystemExit("Database is not migrated. Run `uv run alembic upgrade head` first.")
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == "operator@demo.example")):
            print("Demo data already present.")
            return
        pw = hash_password(PASSWORD)
        op = User(email="operator@demo.example", name="Platform Operator", password_hash=pw, is_platform_admin=True)
        admin = User(email="manager@sugarland-demo.example", name="Dealer Manager", password_hash=pw)
        staff = User(email="sales@sugarland-demo.example", name="Sales Rep", password_hash=pw)
        db.add_all([op, admin, staff])
        d = Dealership(name="Sugar Land Demo Motors (SANDBOX)", slug="sugar-land-demo", status="ACTIVE",
                       phone="(281) 555-0100", address_line1="100 Example Pkwy", city="Sugar Land", state="TX",
                       postal_code="77478", business_info_approved=True, business_info_approved_at=utcnow())
        db.add(d)
        db.flush()
        db.add_all([Membership(user_id=admin.id, dealership_id=d.id, role="DEALER_ADMIN", receives_escalations=True),
                    Membership(user_id=staff.id, dealership_id=d.id, role="DEALER_STAFF"),
                    AutomationSettings(dealership_id=d.id)])
        for wd in range(7):
            db.add(BusinessHours(dealership_id=d.id, weekday=wd, closed=wd == 6, open_time=None if wd == 6 else "09:00",
                                 close_time=None if wd == 6 else ("18:00" if wd == 5 else "19:00")))
        for provider, ext, name in (("WEBSITE_CHAT", f"web:{d.id}", "Website chat"),
                                    ("MARKETPLACE_ASSISTED", f"mp:{d.id}", "Marketplace (human-assisted)")):
            db.add(IntegrationConnection(dealership_id=d.id, provider=provider, external_account_id=ext, display_name=name,
                                         status="CONNECTED"))
        vehicles = [
            Vehicle(dealership_id=d.id, stock_number="A100", vin="2HGFC2F70KH500001", year=2019, make="Honda", model="Civic",
                    trim="EX", price_cents=1899500, mileage=45210, status="AVAILABLE", verified_at=utcnow(),
                    approved_features=["Backup camera", "Bluetooth", "Apple CarPlay", "Sunroof"]),
            Vehicle(dealership_id=d.id, stock_number="B200", year=2017, make="Ford", model="F-150", trim="XLT",
                    price_cents=2750000, mileage=81000, status="AVAILABLE", verified_at=utcnow(),
                    approved_features=["4x4", "Tow package"]),
            Vehicle(dealership_id=d.id, stock_number="D400", year=2018, make="Chevrolet", model="Equinox", trim="LT",
                    price_cents=1645000, mileage=62300, status="AVAILABLE", verified_at=utcnow() - timedelta(days=10)),
        ]
        db.add_all(vehicles)
        db.flush()
        db.add(ListingDraft(dealership_id=d.id, vehicle_id=vehicles[0].id, language="es",
                            title="Honda Civic EX 2019 — 45,210 millas",
                            body="Sample Phase 2 placeholder draft for review.", generated_by="seed"))
        db.add(Campaign(name="Sugar Land independents — intro", steps=DEFAULT_STEPS))
        db.add(OutreachFAQ(question="What does it cost?", answer="Pricing depends on inventory size; we share a written "
                           "quote after a short call.", keywords=["price", "pricing", "cost", "how much"],
                           approved_by_id=op.id, approved_at=utcnow()))
        db.commit()
        print("Seeded SANDBOX demo. Logins (password: %s):" % PASSWORD)
        print("  operator@demo.example (platform admin) · manager@sugarland-demo.example · sales@sugarland-demo.example")
        print(f"  Website chat test page: /chat/{d.public_chat_key}")


if __name__ == "__main__":
    main()
