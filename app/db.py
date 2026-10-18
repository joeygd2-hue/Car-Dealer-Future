"""Database engine, session factory and tenant isolation.

Tenant isolation is enforced in three places:

1. Schema: every dealership-owned table carries ``client_id`` and children
   reference parents through composite ``(client_id, id)`` foreign keys, so a row
   can never point at another dealership's parent row.
2. ORM: a session bound to a tenant (``tenant_session``) automatically adds
   ``client_id = :tenant`` to every SELECT/UPDATE/DELETE on tenant models and
   refuses to flush rows belonging to another tenant or rows whose client_id was
   changed.
3. API: routes obtain a tenant session only through ``authorize_client``
   (app/core/tenancy.py), which checks the caller's membership server-side.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import DateTime, String, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker, with_loader_criteria
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.core.time import as_utc


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return as_utc(value)

    def process_result_value(self, value, dialect):
        return as_utc(value)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


class TenantMixin:
    """Marks a model as owned by one dealership (``clients.id``)."""
    client_id: Mapped[str] = mapped_column(String(40), index=True)


class TenantViolation(Exception):
    pass


def make_engine(url: str | None = None):
    url = url or settings.database_url
    kwargs = {}
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def configure(url: str):
    """Point the app at a different database (used by tests and scripts)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
    return engine


def init_db() -> None:
    """Create tables directly (tests/dev). Production uses Alembic migrations."""
    import app.models  # noqa: F401  (register models)
    Base.metadata.create_all(engine)


@event.listens_for(Session, "do_orm_execute")
def _scope_to_tenant(state):
    tenant = state.session.info.get("tenant_id")
    if tenant and not state.is_column_load and not state.is_relationship_load:
        state.statement = state.statement.options(
            with_loader_criteria(TenantMixin, lambda cls: cls.client_id == tenant, include_aliases=True)
        )


@event.listens_for(Session, "before_flush")
def _check_tenant_writes(session, flush_context, instances):
    tenant = session.info.get("tenant_id")
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if not isinstance(obj, TenantMixin):
            continue
        hist = inspect(obj).attrs.client_id.history
        if obj not in session.new and hist.deleted and hist.added and hist.deleted[0] != hist.added[0]:
            raise TenantViolation("client_id is immutable")
        if tenant and obj.client_id != tenant:
            raise TenantViolation(f"{type(obj).__name__} belongs to a different dealership")


def tenant_session(client_id: str) -> Session:
    s = SessionLocal()
    s.info["tenant_id"] = client_id
    return s


@contextmanager
def session_scope(client_id: str | None = None) -> Iterator[Session]:
    s = tenant_session(client_id) if client_id else SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
