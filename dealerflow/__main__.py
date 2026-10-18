"""Command line: python -m dealerflow <command>

  migrate                      apply database migrations and seed defaults
  create-user EMAIL ROLE       create a user (prompts for password); ROLE = viewer|rep|manager|admin
  web                          run the web app
  worker                       run the background worker (agent ticks + jobs)
  run-jobs                     run due jobs once and exit
  demo                         load SIMULATED fixture dealerships (no network)
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
    from .models import ROLES, User
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
