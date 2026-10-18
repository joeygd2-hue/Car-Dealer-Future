"""Seed a local database with CLEARLY LABELLED fixture data for demos and manual testing.

Creates: a platform admin, a demo dealership "DEMO - Fixture Motors (test data)" with a dealer admin,
fixture inventory (fixtures/demo-inventory.csv: fake stock numbers, picsum.photos placeholder
images), sandbox outreach integrations, approved company facts and a *draft* sandbox campaign.

Nothing here contacts real dealerships or publishes anything. Usage:
    python -m scripts.seed_fixtures            # passwords printed once
    SEED_ADMIN_PASSWORD=... python -m scripts.seed_fixtures
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.core.security import create_user
from app.core.tenancy import job_context
from app.inventory.importer import run_import
from app.inventory.parsers import parse_csv
from app.listings.dealer_settings import update_settings
from app.models import Campaign, Client, ClientMembership, CompanyFact, InventorySource, PlatformIntegration, User

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    init_db()
    db = SessionLocal()
    created = {}
    admin = db.scalar(select(User).where(User.email == "admin@example.test"))
    if not admin:
        pw = os.environ.get("SEED_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        admin = create_user(db, email="admin@example.test", name="Platform Admin (demo)", password=pw, platform_role="platform_admin")
        created["admin@example.test"] = pw
    client = db.scalar(select(Client).where(Client.slug == "demo-fixture-motors"))
    if not client:
        client = Client(name="DEMO - Fixture Motors (test data)", slug="demo-fixture-motors", city="Sugar Land", state="TX")
        db.add(client)
        db.flush()
        pw = os.environ.get("SEED_DEALER_PASSWORD") or secrets.token_urlsafe(12)
        dealer = create_user(db, email="dealer@example.test", name="Demo Dealer Admin", password=pw)
        db.add(ClientMembership(client_id=client.id, user_id=dealer.id, role="dealer_admin"))
        created["dealer@example.test"] = pw
    if not db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == "email")):
        for kind, name in (("email", "Sandbox mailbox"), ("calendar", "Sandbox calendar"), ("discovery", "Sandbox discovery")):
            db.add(PlatformIntegration(kind=kind, provider="sandbox", name=name, health_status="ok"))
        db.add(CompanyFact(topic="company_overview", keywords=[], approved_by=admin.id,
                           answer="We prepare accurate English and Spanish vehicle listings for independent dealerships and help publish them."))
        db.add(CompanyFact(topic="pricing", keywords=["price", "pricing", "cost", "how much"], approved_by=admin.id,
                           answer="Pricing depends on inventory size; we send a written quote after a short call."))
        db.flush()
        email = db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == "email"))
        cal = db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == "calendar"))
        db.add(Campaign(name="DEMO sandbox campaign", status="draft", sender_name="Demo Sender", sender_email="sender@example.test",
                        postal_address="100 Example Rd, Sugar Land, TX 77478 (demo address)", email_integration_id=email.id,
                        calendar_integration_id=cal.id, target_criteria={"min_queue": 10, "queries": ["used car dealer in Sugar Land, TX"]}))
    db.commit()

    ctx = job_context(client.id, "seed")
    update_settings(ctx, {"contact": {"dealership_name": "Fixture Motors (demo)", "phone": "(281) 555-0100", "email": "sales@example.test",
                                      "website": "", "address": "100 Fixture Ln, Sugar Land, TX 77478"},
                          "business_hours": {"en": "Mon-Sat 9am-7pm", "es": "Lun-Sáb 9am-7pm"},
                          "disclaimers": [{"en": "Price excludes tax, title and license.", "es": "El precio no incluye impuestos, título ni placas."}],
                          "enabled_channels": ["facebook_marketplace", "sandbox"]})
    src = ctx.db.scalar(select(InventorySource).where(InventorySource.name == "Fixture CSV"))
    if not src:
        src = InventorySource(client_id=client.id, name="Fixture CSV", source_type="csv_upload",
                              authorization_note="FIXTURE DATA - not a real dealership")
        ctx.db.add(src)
        ctx.db.flush()
    data = (ROOT / "fixtures" / "demo-inventory.csv").read_bytes()
    headers, rows = parse_csv(data)
    run = run_import(ctx, src, headers, rows, trigger="manual", file_name="demo-inventory.csv", file_bytes=data)
    ctx.db.commit()
    print(f"Fixture import: {run.status} {dict((k, v) for k, v in run.counts.items() if isinstance(v, int))}")
    print("Run the worker (python -m app.worker) to generate listing drafts.")
    for email_, pw in created.items():
        print(f"Created login {email_} / {pw}")


if __name__ == "__main__":
    main()
