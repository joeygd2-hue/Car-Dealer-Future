"""Application configuration.

All secrets and environment-specific values come from environment variables
(or a local `.env` file in development). Nothing secret is committed.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    environment: str = Field("development", description="development | test | production")
    secret_key: str = INSECURE_DEFAULT_SECRET
    database_url: str = "sqlite:///./data/app.db"
    base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    session_ttl_hours: int = 12
    attachments_dir: str = "./data/attachments"
    max_upload_bytes: int = 10 * 1024 * 1024
    default_currency: str = "USD"
    business_timezone: str = "America/Chicago"

    # Credential encryption (Fernet key, urlsafe base64 32 bytes). Required in production.
    credentials_encryption_key: str = ""

    # Rate limiting
    login_rate_limit_per_minute: int = 10
    api_rate_limit_per_minute: int = 300

    # Company identity used in outreach (CAN-SPAM requires a valid postal address)
    company_name: str = "Dealer Growth Services"
    company_postal_address: str = ""
    company_reply_to: str = ""
    outreach_from_address: str = ""

    # Email delivery: "sandbox" (store only, never sent) | "smtp"
    email_provider: str = "sandbox"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    # Inbox monitoring: "sandbox" | "imap"
    inbox_provider: str = "sandbox"
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    inbound_webhook_secret: str = ""

    # Calendar: "sandbox" | "google"
    calendar_provider: str = "sandbox"
    google_calendar_id: str = "primary"
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""

    # Business-data sources
    google_places_api_key: str = ""
    pagespeed_api_key: str = ""

    # Payments / accounting
    stripe_webhook_secret: str = ""

    # AI provider (optional). When unset, deterministic templates/rules are used.
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5-5"
    ai_enabled: bool = False

    # Outbound HTTP politeness for crawling/enrichment
    crawler_user_agent: str = "DealerGrowthAuditBot/1.0 (+contact: set CRAWLER_CONTACT)"
    crawler_max_pages: int = 50
    crawler_delay_seconds: float = 1.0
    crawler_timeout_seconds: float = 15.0

    # Worker
    worker_poll_seconds: float = 5.0
    job_lease_seconds: int = 300

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def production_problems(self) -> list[str]:
        """Configuration errors that must block a production start."""
        problems: list[str] = []
        if self.secret_key == INSECURE_DEFAULT_SECRET or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to a random value of at least 32 characters")
        if not self.credentials_encryption_key:
            problems.append("CREDENTIALS_ENCRYPTION_KEY must be set")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL should point to PostgreSQL in production")
        if not self.base_url.startswith("https://"):
            problems.append("BASE_URL must use https in production")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
