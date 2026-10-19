"""Run Alembic migrations programmatically (``python -m dealerflow migrate``)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import get_settings

HERE = Path(__file__).resolve().parent


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """``legacy_*`` tables are the read-only archive kept by migration 0005; they have no model on purpose."""
    return not (type_ == "table" and reflected and compare_to is None and str(name).startswith("legacy_"))


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().database_url)
    return cfg


def upgrade(url: str | None = None) -> None:
    url = url or get_settings().database_url
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(url), "head")


def revision(message: str) -> None:
    command.revision(alembic_config(), message=message, autogenerate=True)
