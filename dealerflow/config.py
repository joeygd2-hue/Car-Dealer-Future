"""Settings, read from environment variables (prefix ``DF_``) or a ``.env`` file.

Secrets live only in the environment. Nothing here is written to the database.
See docs/architecture/environment.md for every variable.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Secrets can come from files instead of environment variables: one file per setting, named like the
    # variable in lower case (e.g. /run/secrets/df_smtp_password). Docker/Compose secrets mount there.
    model_config = SettingsConfigDict(env_prefix="DF_", env_file=".env", extra="ignore",
                                      secrets_dir=os.environ.get("DF_SECRETS_DIR", "/run/secrets")
                                      if os.path.isdir(os.environ.get("DF_SECRETS_DIR", "/run/secrets")) else None)

    env: str = "development"  # development | test | production
    database_url: str = "sqlite:///./state/dealerflow.db"
    secret_key: SecretStr = SecretStr("")  # required outside development/test
    base_url: str = "http://127.0.0.1:8000"
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    log_level: str = "INFO"
    timezone: str = "America/Chicago"

    # Business identity used in outreach (CAN-SPAM requires a valid physical postal address).
    business_name: str = ""
    business_postal_address: str = ""
    sender_name: str = ""
    sender_email: str = ""

    # Target market: centre point and radius for geographic fit (default Sugar Land, TX city hall).
    target_lat: float = 29.6197
    target_lng: float = -95.6349
    target_radius_miles: float = 30.0
    target_cities: str = (
        "Sugar Land,Stafford,Missouri City,Richmond,Rosenberg,Houston,Katy,Pearland,Fulshear,Meadows Place"
    )

    # Providers. "mock" providers are SIMULATED and never contact anyone.
    email_provider: str = "mock"  # mock | smtp | gmail
    calendar_provider: str = "mock"  # mock | google
    zoom_provider: str = "mock"  # mock | zoom
    call_provider: str = "manual"  # manual (tel: link) | twilio | mock

    # Outbound safety. Sending stays off until explicitly enabled.
    outreach_sending_enabled: bool = False
    outreach_daily_cap: int = 25
    outreach_per_prospect_cooldown_days: int = 7
    outreach_max_touches_per_prospect: int = 3

    # SMTP / IMAP (any mailbox that allows it, e.g. Google Workspace with an app password)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = True
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: SecretStr = SecretStr("")
    imap_folder: str = "INBOX"

    # Google OAuth (Gmail + Calendar). Refresh token obtained once via scripts/google_oauth.py.
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_refresh_token: SecretStr = SecretStr("")
    google_calendar_id: str = "primary"
    gmail_user: str = "me"

    # Zoom Server-to-Server OAuth app
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: SecretStr = SecretStr("")
    zoom_user_id: str = "me"
    zoom_webhook_secret_token: SecretStr = SecretStr("")

    # Twilio click-to-call (human-operated bridge; never auto-dials prospects)
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_caller_id: str = ""  # a number you own or verified in Twilio
    agent_phone: str = ""  # your phone; Twilio rings this first
    calling_hours_start: int = 9  # local time, inclusive
    calling_hours_end: int = 21  # local time, exclusive
    calling_sunday_start: int = 12

    # Lead discovery
    google_places_api_key: SecretStr = SecretStr("")  # client exists; results may not be stored as leads
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    website_research_enabled: bool = False
    http_user_agent: str = "dealerflow-research/0.1 (+contact: set DF_SENDER_EMAIL)"

    # Meetings
    meeting_length_minutes: int = 30
    meeting_buffer_minutes: int = 15
    working_hours_start: int = 9
    working_hours_end: int = 17
    working_days: str = "0,1,2,3,4"  # Monday=0

    # Phase 2: inventory, listings, publishing
    max_upload_bytes: int = 10 * 1024 * 1024
    max_feed_bytes: int = 25 * 1024 * 1024
    feed_timeout_seconds: float = 30.0
    publish_max_attempts: int = 5
    # Listing prose is written by the deterministic template generator unless this, DF_AI_ENABLED and a key are all set.
    listing_ai_enabled: bool = False
    # Nothing is sent to Meta unless this is true AND the dealership's connection is authorized for live publishing.
    meta_live_publishing_allowed: bool = False
    meta_graph_api_version: str = "v24.0"
    meta_graph_base_url: str = "https://graph.facebook.com"

    # Notifications
    notify_email: str = ""  # optional daily digest recipient; empty disables it
    # Inboxes you control, comma separated. `live-check` may send real test emails ONLY to these, even
    # before live operation is authorized. Never put a prospect's address here.
    test_inboxes: str = ""

    @property
    def test_inbox_list(self) -> list[str]:
        return [x.strip().lower() for x in self.test_inboxes.split(",") if x.strip()]


    session_max_age_seconds: int = Field(default=60 * 60 * 12)

    # --- Phase 4 ---------------------------------------------------------------------------
    # Client-facing email (monthly reports, lead summaries). Off by default: messages are recorded
    # as SIMULATED. Real delivery also needs the client's "report_email" connection marked connected.
    client_email_sending_enabled: bool = False
    attachments_dir: str = "./state/attachments"
    api_rate_limit_per_minute: int = 300
    crawler_timeout_seconds: float = 15.0
    pagespeed_api_key: SecretStr = SecretStr("")  # Google PageSpeed Insights API v5 (optional)
    stripe_webhook_secret: SecretStr = SecretStr("")  # whsec_... ; empty disables the Stripe webhook
    retention_days_conversations: int = 365

    # --- AI (shared by all phases) ---------------------------------------------------------
    # Master switch for any Claude API call. Off by default: deterministic templates/rules are used instead.
    ai_enabled: bool = False
    anthropic_api_key: SecretStr = SecretStr("")
    ai_model: str = "claude-opus-5-5"
    ai_effort: str = "medium"

    @property
    def ai_available(self) -> bool:
        return self.ai_enabled and bool(self.anthropic_api_key.get_secret_value())

    @property
    def listing_ai_available(self) -> bool:
        return self.listing_ai_enabled and self.ai_available

    @property
    def target_city_list(self) -> list[str]:
        return [c.strip() for c in self.target_cities.split(",") if c.strip()]

    @property
    def working_day_list(self) -> list[int]:
        return [int(d) for d in self.working_days.split(",") if d.strip()]

    def production_problems(self) -> list[str]:
        """Configuration that blocks startup with DF_ENV=production (see production_warnings for advisories)."""
        problems = []
        if len(self.secret_key.get_secret_value()) < 32:
            problems.append("DF_SECRET_KEY must be a random value of at least 32 characters (python -c 'import secrets; print(secrets.token_urlsafe(48))')")
        if (self.outreach_sending_enabled or self.client_email_sending_enabled) and not self.base_url.startswith("https://"):
            problems.append("DF_BASE_URL must be https:// before real email is enabled (unsubscribe links must work for recipients)")
        return problems

    def production_warnings(self) -> list[str]:
        warnings = []
        if not self.base_url.startswith("https://"):
            warnings.append("DF_BASE_URL is not https://; webhooks and unsubscribe links will not work publicly")
        if self.database_url.startswith("sqlite"):
            warnings.append("SQLite is fine for one host; use PostgreSQL for multiple web/worker hosts or managed backups")
        return warnings

    def require_secret_key(self) -> str:
        key = self.secret_key.get_secret_value()
        if key:
            return key
        if self.env in ("development", "test"):
            return "insecure-dev-key-do-not-use-in-production"
        raise RuntimeError("DF_SECRET_KEY must be set outside development")


@lru_cache
def get_settings() -> Settings:
    return Settings()
