"""Central configuration, read from environment variables (see .env.example).

Secrets are never hard-coded. A local ``.env`` file is loaded for convenience;
real environment variables always take precedence.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v in (None, "") else v.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.environ.get("APP_ENV", "development"))
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./data/app.db"))
    session_ttl_hours: int = field(default_factory=lambda: _int("SESSION_TTL_HOURS", 12))
    # base64-encoded 32-byte key used to encrypt stored provider credentials.
    encryption_key: str = field(default_factory=lambda: os.environ.get("APP_ENCRYPTION_KEY", ""))
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
    cors_origins: str = field(default_factory=lambda: os.environ.get("CORS_ORIGINS", ""))

    # Listing AI. Template generator is used unless AI is enabled AND a key exists.
    listing_ai_enabled: bool = field(default_factory=lambda: _bool("LISTING_AI_ENABLED"))
    ai_model: str = field(default_factory=lambda: os.environ.get("AI_MODEL", "claude-opus-5-5"))
    ai_effort: str = field(default_factory=lambda: os.environ.get("AI_EFFORT", "medium"))

    # Background worker.
    worker_poll_seconds: float = field(default_factory=lambda: float(os.environ.get("WORKER_POLL_SECONDS", "2")))
    feed_timeout_seconds: int = field(default_factory=lambda: _int("FEED_TIMEOUT_SECONDS", 30))
    max_feed_bytes: int = field(default_factory=lambda: _int("MAX_FEED_BYTES", 25 * 1024 * 1024))
    publish_max_attempts: int = field(default_factory=lambda: _int("PUBLISH_MAX_ATTEMPTS", 5))

    # Meta. Nothing is sent to Meta unless this global switch is on AND the
    # dealership's connection is explicitly authorised for live publishing.
    meta_graph_api_version: str = field(default_factory=lambda: os.environ.get("META_GRAPH_API_VERSION", "v24.0"))
    meta_graph_base_url: str = field(default_factory=lambda: os.environ.get("META_GRAPH_BASE_URL", "https://graph.facebook.com"))
    meta_live_publishing_allowed: bool = field(default_factory=lambda: _bool("META_LIVE_PUBLISHING_ALLOWED"))

    # Outreach agent. Live sending requires this AND a human authorisation
    # recorded in the database (see app/outreach/policy.py).
    outreach_live_allowed: bool = field(default_factory=lambda: _bool("OUTREACH_LIVE_ALLOWED"))
    outreach_ai_enabled: bool = field(default_factory=lambda: _bool("OUTREACH_AI_ENABLED"))
    # Public base URL used in unsubscribe links (must be reachable by recipients).
    public_base_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"))
    # Secret for signing unsubscribe tokens.
    unsubscribe_secret: str = field(default_factory=lambda: os.environ.get("UNSUBSCRIBE_SECRET", ""))
    agent_tick_seconds: int = field(default_factory=lambda: _int("AGENT_TICK_SECONDS", 120))

    @property
    def ai_available(self) -> bool:
        return self.listing_ai_enabled and bool(os.environ.get("ANTHROPIC_API_KEY"))


settings = Settings()


def reload_settings() -> Settings:
    global settings
    settings = Settings()
    return settings
