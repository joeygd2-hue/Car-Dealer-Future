"""Command line: python -m dealerflow <command>

  migrate                      apply database migrations and seed defaults
  create-user EMAIL ROLE       create a user (prompts for password); ROLE = viewer|rep|manager|admin
  web                          run the web app
  worker                       run the background worker (agent ticks + jobs)
  run-jobs                     run due jobs once and exit
  demo                         load SIMULATED fixture dealerships (no network)
  demo-dealer                  create a SIMULATED dealership client with fixture inventory (Phase 2 demo)
  demo-conversations           add SIMULATED website-chat inquiries to the demo dealership (Phase 3 demo; run demo-dealer first)
  check-integrations           verify configured provider credentials without sending anything
  backup DIR                   write a verified database backup to DIR
  restore FILE --yes           replace the database with a backup (stop web + worker first)
  live-check send|inbox|meeting  controlled real-provider test (see dealerflow/livecheck.py)
  retention [DAYS]             redact message bodies older than DAYS (default 730) and prune old logs
"""

from __future__ import annotations

import getpass
import sys

from .config import get_settings
from .logging_setup import configure_logging


def _migrate() -> None:
    from .db import session_scope
    from .migrate import upgrade
    from .seed import seed

    upgrade()
    with session_scope() as db:
        seed(db)
    print("database migrated and seeded")


def _create_user(email: str, role: str) -> None:
    from .db import session_scope
    from .models import STAFF_ROLES as ROLES, User
    from .security import hash_password

    if role not in ROLES:
        sys.exit(f"role must be one of {ROLES}")
    pw = getpass.getpass("Password (min 10 chars): ")
    with session_scope() as db:
        db.add(User(email=email.lower(), name=email.split("@")[0], role=role, password_hash=hash_password(pw)))
    print(f"created {role} {email}")


def _demo() -> None:
    from .db import session_scope
    from .services.discovery import run_source

    with session_scope() as db:
        print(run_source(db, "fixture"))


def _demo_dealer() -> None:
    from pathlib import Path

    from sqlalchemy import select

    from .db import session_scope
    from .models_dealer import Client, InventorySource
    from .services.inventory.importer import run_import
    from .services.inventory.parsers import parse_csv
    from .services.listings.dealer_settings import update_settings
    from .tenancy import system_context

    with session_scope() as db:
        c = db.scalar(select(Client).where(Client.slug == "demo-fixture-motors"))
        if not c:
            c = Client(name="DEMO Fixture Motors (SIMULATED)", slug="demo-fixture-motors", city="Sugar Land", state="TX", simulated=True)
            db.add(c)
            db.flush()
        cid = c.id
    ctx = system_context(cid)
    try:
        update_settings(ctx, {"contact": {"dealership_name": "Fixture Motors (demo)", "phone": "(281) 555-0100",
                                          "email": "sales@fixture.example", "website": "", "address": "100 Fixture Ln, Sugar Land, TX 77478"},
                              "business_hours": {"en": "Mon-Sat 9am-7pm", "es": "Lun-Sáb 9am-7pm"},
                              "disclaimers": [{"en": "Price excludes tax, title and license.",
                                               "es": "El precio no incluye impuestos, título ni placas."}],
                              "enabled_channels": ["facebook_marketplace", "mock"]})
        src = ctx.db.scalar(select(InventorySource).where(InventorySource.name == "Fixture CSV"))
        if not src:
            src = InventorySource(client_id=cid, name="Fixture CSV", source_type="csv_upload",
                                  authorization_note="FIXTURE DATA - not a real dealership")
            ctx.db.add(src)
            ctx.db.flush()
        data = (Path(__file__).parent / "fixtures" / "demo_inventory.csv").read_bytes()
        headers, rows = parse_csv(data)
        run = run_import(ctx, src, headers, rows, trigger="manual", file_name="demo_inventory.csv", file_bytes=data)
        ctx.db.commit()
        print(f"dealership {cid}: import {run.status}; run the worker (or run-jobs) to generate listing drafts")
    finally:
        ctx.close()


def _demo_conversations() -> None:
    from sqlalchemy import select

    from .db import session_scope
    from .models import Client, IntegrationConnection, ResponseAutomationSettings
    from .services.conversations.processing import ingest_inbound
    from .services.conversations.seeding import set_weekly_hours

    with session_scope() as db:
        c = db.scalar(select(Client).where(Client.slug == "demo-fixture-motors"))
        if c is None:
            sys.exit("run `python -m dealerflow demo-dealer` first")
        c.phone, c.address_line, c.postal_code = c.phone or "(281) 555-0100", c.address_line or "100 Fixture Ln", c.postal_code or "77478"
        c.business_info_approved = True
        if not (c.business_hours or {}).get("weekly"):
            set_weekly_hours(c, [["09:00", "19:00"]] * 6 + [None])
        if db.scalar(select(ResponseAutomationSettings).where(ResponseAutomationSettings.client_id == c.id)) is None:
            db.add(ResponseAutomationSettings(client_id=c.id))
        if db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == c.id,
                                                         IntegrationConnection.kind == "customer_messaging",
                                                         IntegrationConnection.provider == "WEBSITE_CHAT")) is None:
            db.add(IntegrationConnection(client_id=c.id, kind="customer_messaging", provider="WEBSITE_CHAT", status="sandbox"))
        db.flush()
        for visitor, text in (("demo-visitor-1", "Hi, is the Camry still available? What's the price?"),
                              ("demo-visitor-2", "¿Puedo hacer una prueba de manejo del F-150 el sábado a las 11? Soy Luis, 832-555-0102"),
                              ("demo-visitor-3", "Do you offer financing? My credit isn't great.")):
            ingest_inbound(db, dealership=c, channel="WEBSITE_CHAT", external_conversation_id=f"web:{visitor}", text=text,
                           provider_message_id=f"demo:{visitor}", simulated=True)
        print(f"dealership {c.id}: 3 SIMULATED inquiries queued; run the worker (or run-jobs) to draft replies. "
              f"Inbox: /dealers/{c.id}/inbox  Test chat page: /chat/{c.public_chat_key}")


def _check() -> None:
    from .integrations.calendar import get_calendar_provider
    from .integrations.email import get_email_provider
    from .integrations.zoom import get_zoom_provider

    for label, factory in (("email", get_email_provider), ("calendar", get_calendar_provider), ("zoom", get_zoom_provider)):
        try:
            print(f"{label}: {factory().check()}")
        except Exception as exc:  # noqa: BLE001
            print(f"{label}: FAILED — {type(exc).__name__}: {exc}")


def main(argv: list[str]) -> None:
    configure_logging(get_settings().log_level)
    cmd = argv[0] if argv else "web"
    if cmd == "migrate":
        _migrate()
    elif cmd == "create-user":
        _create_user(argv[1], argv[2])
    elif cmd == "web":
        import uvicorn

        s = get_settings()
        uvicorn.run("dealerflow.web.app:create_app", factory=True, host=s.web_host, port=s.web_port, log_config=None)
    elif cmd == "worker":
        from .worker import main as worker_main

        worker_main()
    elif cmd == "run-jobs":
        from .worker import run_once

        print(run_once())
    elif cmd == "demo":
        _demo()
    elif cmd == "demo-dealer":
        _demo_dealer()
    elif cmd == "demo-conversations":
        _demo_conversations()
    elif cmd == "check-integrations":
        _check()
    elif cmd == "backup":
        from .backup import backup

        print(backup(argv[1] if len(argv) > 1 else "./backups"))
    elif cmd == "live-check":
        from . import handlers  # noqa: F401
        from . import livecheck

        sub = argv[1] if len(argv) > 1 else ""
        if sub == "send":
            print(livecheck.send(argv[argv.index("--to") + 1], confirmed="--i-control-this-inbox" in argv))
        elif sub == "inbox":
            print(livecheck.inbox())
        elif sub == "meeting":
            print(livecheck.meeting())
        else:
            print(livecheck.__doc__)
    elif cmd == "retention":
        from .db import session_scope
        from .retention import apply

        with session_scope() as db:
            print(apply(db, message_days=int(argv[1]) if len(argv) > 1 else 730))
    elif cmd == "restore":
        from .backup import restore

        print(restore(argv[1], confirm="--yes" in argv))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
