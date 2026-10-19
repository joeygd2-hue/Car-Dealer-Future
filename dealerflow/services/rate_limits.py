"""Distributed fixed-window counters (shared by all web + worker processes through the database). Ported from Phase 3.

``try_acquire`` is a single conditional UPDATE (``count + n <= limit``), so concurrent workers can
never exceed a limit: on PostgreSQL the row lock serializes them, on SQLite the database write lock does.
Callers should commit soon after acquiring (typically together with the send claim).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import RateCounter


def window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = datetime(1970, 1, 1)
    secs = int((now - epoch).total_seconds())
    return epoch + timedelta(seconds=secs - secs % window_seconds)


def _ensure(db: Session, key: str, start: datetime) -> None:
    try:
        with db.begin_nested():
            db.add(RateCounter(key=key, window_start=start, count=0))
            db.flush()
    except IntegrityError:
        pass


def try_acquire(db: Session, key: str, limit: int, window_seconds: int, amount: int = 1,
                now: datetime | None = None) -> bool:
    if limit <= 0:
        return False
    start = window_start(now or utcnow(), window_seconds)
    _ensure(db, key, start)
    res = db.execute(
        update(RateCounter)
        .where(RateCounter.key == key, RateCounter.window_start == start, RateCounter.count + amount <= limit)
        .values(count=RateCounter.count + amount)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1


def release(db: Session, key: str, window_seconds: int, amount: int = 1, now: datetime | None = None) -> None:
    start = window_start(now or utcnow(), window_seconds)
    db.execute(update(RateCounter).where(RateCounter.key == key, RateCounter.window_start == start,
                                         RateCounter.count >= amount)
               .values(count=RateCounter.count - amount).execution_options(synchronize_session=False))


def current(db: Session, key: str, window_seconds: int, now: datetime | None = None) -> int:
    from sqlalchemy import select

    start = window_start(now or utcnow(), window_seconds)
    return int(db.scalar(select(RateCounter.count).where(RateCounter.key == key, RateCounter.window_start == start)) or 0)


def acquire_all(db: Session, specs: list[tuple[str, int, int, int]]) -> str | None:
    """Acquire several (key, limit, window, amount) counters; on the first failure release the earlier ones.
    Returns the key that failed, or None when all were acquired."""
    done = []
    for key, limit, window, amount in specs:
        if not try_acquire(db, key, limit, window, amount):
            for k, _l, w, a in done:
                release(db, k, w, a)
            return key
        done.append((key, limit, window, amount))
    return None


def hit(key: str, limit: int, window_seconds: int) -> bool:
    """Acquire one slot in a short transaction of its own (for request-level limits outside a job/session)."""
    from ..db import session_scope

    with session_scope() as db:
        ok = try_acquire(db, key, limit, window_seconds)
    return ok


def purge_old(db: Session, older_than: timedelta = timedelta(days=2)) -> int:
    from sqlalchemy import delete

    return db.execute(delete(RateCounter).where(RateCounter.window_start < utcnow() - older_than)).rowcount or 0
