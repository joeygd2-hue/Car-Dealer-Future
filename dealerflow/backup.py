"""Database backup and restore.

SQLite: uses SQLite's online backup API (safe while the web app and worker are running), then
checks the copy with ``PRAGMA integrity_check`` and reports row counts.
Postgres: wraps ``pg_dump --format=custom`` / ``pg_restore`` (client tools must be installed).

Backups contain prospect data and the audit trail but never secrets (secrets live only in the
environment). Store them encrypted and off the host; see docs/audit/security-findings.md.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from .config import get_settings

COUNT_TABLES = ("prospects", "outreach_messages", "inbound_messages", "suppressions", "meetings", "audit_log", "users")


def _sqlite_path(url: str) -> Path:
    return Path(url.removeprefix("sqlite:///"))


def _counts_sqlite(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"integrity check failed for {path}: {ok}")
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES}  # noqa: S608
    finally:
        con.close()


def backup(dest_dir: str, url: str | None = None) -> dict:
    url = url or get_settings().database_url
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    if url.startswith("sqlite:///"):
        src = _sqlite_path(url)
        dest = Path(dest_dir) / f"dealerflow-{stamp}.db"
        s, d = sqlite3.connect(src), sqlite3.connect(dest)
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")  # self-contained single file, no -wal/-shm side files
        finally:
            d.close()
            s.close()
        os.chmod(dest, 0o600)
        return {"file": str(dest), "counts": _counts_sqlite(dest)}
    if url.startswith("postgresql"):
        dest = Path(dest_dir) / f"dealerflow-{stamp}.dump"
        u = make_url(url)
        env = {**os.environ, "PGPASSWORD": u.password or ""}
        subprocess.run(["pg_dump", "--format=custom", "--no-owner", "-h", u.host or "localhost", "-p", str(u.port or 5432),
                        "-U", u.username or "", "-f", str(dest), u.database or ""], check=True, env=env)
        os.chmod(dest, 0o600)
        return {"file": str(dest)}
    raise ValueError(f"backup not supported for {url.split(':')[0]}")


def restore(src_file: str, url: str | None = None, *, confirm: bool = False) -> dict:
    """Replace the current database with a backup. Stop the web app and worker first."""
    if not confirm:
        raise ValueError("restore replaces the current database; pass --yes after stopping web and worker")
    url = url or get_settings().database_url
    if url.startswith("sqlite:///"):
        counts = _counts_sqlite(Path(src_file))  # verify before touching the live file
        target = _sqlite_path(url)
        if target.exists():
            shutil.copy2(target, str(target) + ".pre-restore")
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        shutil.copy2(src_file, target)
        return {"restored": str(target), "counts": _counts_sqlite(target), "backup_counts": counts}
    if url.startswith("postgresql"):
        u = make_url(url)
        env = {**os.environ, "PGPASSWORD": u.password or ""}
        subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "-h", u.host or "localhost",
                        "-p", str(u.port or 5432), "-U", u.username or "", "-d", u.database or "", src_file],
                       check=True, env=env)
        return {"restored": u.database}
    raise ValueError(f"restore not supported for {url.split(':')[0]}")
