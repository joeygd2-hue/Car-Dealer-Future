"""Operations CLI: `python -m app.cli <command>`."""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.main import _register_job_handlers


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("create-user", help="Create a staff or admin user")
    u.add_argument("email")
    u.add_argument("--role", choices=["owner", "admin", "staff"], default="admin")
    sub.add_parser("bootstrap", help="Seed reference data and scheduled tasks (idempotent)")
    r = sub.add_parser("run-jobs", help="Run due jobs once and exit")
    r.add_argument("--limit", type=int, default=100)
    sub.add_parser("check-config", help="Report production configuration problems")
    e = sub.add_parser("export-client", help="Write all data owned by a client as JSON to stdout")
    e.add_argument("client_id", type=int)
    d = sub.add_parser("delete-client", help="Export then permanently delete a client")
    d.add_argument("client_id", type=int)
    d.add_argument("--confirm-name", required=True)
    d.add_argument("--export-dir", default="./data/exports")
    d.add_argument("--actor-email", required=True)
    rp = sub.add_parser("purge-retention", help="Purge conversation PII older than N days")
    rp.add_argument("--days", type=int, default=365)
    sub.add_parser("generate-key", help="Print a new Fernet key for CREDENTIALS_ENCRYPTION_KEY")
    args = p.parse_args(argv)

    if args.cmd == "generate-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0
    if args.cmd == "check-config":
        from app.config import get_settings

        problems = get_settings().production_problems()
        print("\n".join(problems) or "No blocking production configuration problems found.")
        return 1 if problems else 0

    _register_job_handlers()
    db = SessionLocal()
    try:
        if args.cmd == "create-user":
            from app.models.core import User
            from app.security.crypto import hash_password

            if db.scalar(select(User).where(User.email == args.email.lower())):
                print("User already exists", file=sys.stderr)
                return 1
            pw = getpass.getpass("Password (min 12 chars): ")
            db.add(User(email=args.email.lower(), password_hash=hash_password(pw), role=args.role))
            db.commit()
            print(f"Created {args.role} {args.email}")
        elif args.cmd == "bootstrap":
            from app.services.maintenance import bootstrap

            bootstrap(db)
            print("Bootstrap complete")
        elif args.cmd == "run-jobs":
            from app.services.jobs import run_pending

            print(f"Ran {run_pending(db, 'cli', args.limit)} job(s)")
        elif args.cmd == "export-client":
            from app.services.maintenance import export_client

            json.dump(export_client(db, args.client_id), sys.stdout, default=str, indent=2)
        elif args.cmd == "delete-client":
            from app.models.core import User
            from app.services.maintenance import delete_client

            actor = db.scalar(select(User).where(User.email == args.actor_email.lower()))
            if actor is None or not actor.is_admin:
                print("Actor must be an existing admin", file=sys.stderr)
                return 1
            path = delete_client(db, args.client_id, args.confirm_name, actor, args.export_dir)
            db.commit()
            print(f"Deleted client {args.client_id}; export written to {path}")
        elif args.cmd == "purge-retention":
            from app.services.maintenance import purge_conversation_pii

            n = purge_conversation_pii(db, args.days)
            db.commit()
            print(f"Purged {n} conversation(s)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
