"""Resolve PlatformIntegration rows to provider instances."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.models import PlatformIntegration
from app.outreach.providers.base import ProviderError

PROVIDERS = {
    "email": ("sandbox", "gmail"),
    "calendar": ("sandbox", "google_calendar"),
    "discovery": ("sandbox", "google_places"),
}


def build(db: Session, integration: PlatformIntegration):
    from app.outreach.providers import google, sandbox
    cfg = integration.config or {}
    if integration.provider == "sandbox":
        return {"email": sandbox.SandboxEmailProvider, "calendar": sandbox.SandboxCalendarProvider,
                "discovery": sandbox.SandboxDiscoveryProvider}[integration.kind](db, cfg)
    secret = decrypt_secret(integration.secret_ciphertext) if integration.secret_ciphertext else None
    creds = google.creds_from_secret(secret)
    if integration.provider == "gmail":
        return google.GmailProvider(creds, cfg)
    if integration.provider == "google_calendar":
        return google.GoogleCalendarProvider(creds, cfg)
    if integration.provider == "google_places":
        return google.GooglePlacesProvider(creds.get("api_key", ""), cfg)
    raise ProviderError(f"Unknown provider {integration.provider}")


def is_live(integration: PlatformIntegration) -> bool:
    return integration.provider != "sandbox"
