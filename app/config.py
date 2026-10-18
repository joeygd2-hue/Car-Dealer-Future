"""Application settings.

All secrets come from environment variables (or a local, git-ignored ``.env``).
Nothing in this module should ever be rendered to a browser or included in an AI prompt.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # APP_ENV_FILE lets tests (and containers) choose which dotenv file is read; default ".env".
    model_config = SettingsConfigDict(env_file=os.getenv("APP_ENV_FILE", ".env"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", description="development | test | production")
    app_name: str = "Car Dealer Future"
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./data/app.db"

    # Credential encryption (Fernet key, urlsafe base64, 32 bytes). Also keys the audit-log HMAC.
    encryption_key: str | None = None
    session_ttl_hours: int = 12
    # MFA: None → required for platform admins in production only.
    require_admin_mfa: bool | None = None
    password_reset_ttl_minutes: int = 60

    # Master switch. When false, every provider adapter resolves to its sandbox implementation,
    # regardless of credentials or DB authorizations. Live operation additionally requires an
    # explicit, recorded LiveAuthorization for the relevant scope.
    live_integrations_enabled: bool = False
    # PILOT MODE (default ON): even when live sending is authorized, every live send must go to a
    # recipient on the internal test allowlist (Admin → Pilot console). Turn off only for general availability.
    pilot_mode: bool = True

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

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value, info):
        """A blank `KEY=` line in .env means "not set": use the default instead of failing to parse it."""
        if isinstance(value, str) and value.strip() == "":
            field = cls.model_fields[info.field_name]
            return field.default
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def admin_mfa_required(self) -> bool:
        return self.is_production if self.require_admin_mfa is None else self.require_admin_mfa


class ConfigError(RuntimeError):
    pass


def validate_settings(settings: Settings) -> list[str]:
    """Return warnings; raise ConfigError for settings that are unsafe to run with."""
    errors: list[str] = []
    warnings: list[str] = []
    if settings.app_env not in ("development", "test", "production"):
        errors.append(f"APP_ENV must be development|test|production (got {settings.app_env!r})")
    if settings.encryption_key:
        try:
            from cryptography.fernet import Fernet

            Fernet(settings.encryption_key.encode())
        except (ValueError, TypeError):
            errors.append("ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)")
    elif settings.is_production:
        errors.append("ENCRYPTION_KEY is required in production")
    else:
        warnings.append("ENCRYPTION_KEY not set: provider credentials cannot be stored and audit HMACs use a dev key")
    if settings.is_production:
        if not settings.base_url.startswith("https://"):
            errors.append("BASE_URL must use https:// in production (secure cookies, webhook signatures)")
        if settings.database_url.startswith("sqlite"):
            warnings.append("SQLite in production: use PostgreSQL for multiple web/worker processes")
        if not settings.admin_mfa_required:
            warnings.append("REQUIRE_ADMIN_MFA=false in production")
    if settings.live_integrations_enabled and not settings.pilot_mode:
        warnings.append("LIVE_INTEGRATIONS_ENABLED with PILOT_MODE=false: live sends are NOT restricted to the test allowlist")
    if errors:
        raise ConfigError("; ".join(errors))
    return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()
