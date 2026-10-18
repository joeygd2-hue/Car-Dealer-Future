"""Application settings.

All secrets come from environment variables (or a local, git-ignored ``.env``).
Nothing in this module should ever be rendered to a browser or included in an AI prompt.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", description="development | test | production")
    app_name: str = "Car Dealer Future"
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./data/app.db"

    # Session signing + credential encryption (Fernet key, urlsafe base64, 32 bytes).
    secret_key: str = "dev-only-change-me"
    encryption_key: str | None = None
    session_ttl_hours: int = 12

    # Master switch. When false, every provider adapter resolves to its sandbox implementation,
    # regardless of credentials or DB authorizations. Live operation additionally requires an
    # explicit, recorded LiveAuthorization for the relevant scope.
    live_integrations_enabled: bool = False

    # Optional LLM rephrasing (responses are always grounded + validated; LLM never adds facts).
    llm_enabled: bool = False
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"

    # Meta (Messenger Platform) — platform-level app.
    meta_app_secret: str | None = None
    meta_verify_token: str | None = None
    meta_graph_version: str = "v23.0"

    # Platform notification email (SMTP) — staff alerts and digests.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True

    # Outreach agent sender identity (CAN-SPAM requires a valid physical postal address).
    outreach_sender_name: str | None = None
    outreach_sender_email: str | None = None
    outreach_company_name: str | None = None
    outreach_postal_address: str | None = None
    outreach_imap_host: str | None = None
    outreach_imap_port: int = 993
    outreach_imap_username: str | None = None
    outreach_imap_password: str | None = None
    outreach_imap_folder: str = "INBOX"
    outreach_smtp_host: str | None = None
    outreach_smtp_port: int = 587
    outreach_smtp_username: str | None = None
    outreach_smtp_password: str | None = None

    # Prospect discovery.
    google_places_api_key: str | None = None

    # Calendar / video meetings (shared by test drives and sales meetings).
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_refresh_token: str | None = None  # scopes: calendar.events + calendar.freebusy
    google_calendar_id: str | None = None
    zoom_account_id: str | None = None
    zoom_client_id: str | None = None
    zoom_client_secret: str | None = None

    # Worker.
    worker_poll_seconds: float = 2.0
    worker_lease_seconds: int = 120
    worker_heartbeat_stale_seconds: int = 90

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
