"""Authorised inventory feed retrieval (feed_api, website_feed, dms_export via URL).

Only structured feeds the dealership (or its DMS / website / syndication provider) has authorised us
to read: CSV, JSON or XLSX over HTTPS. A feed credential is read from the environment variable named in
``InventorySource.secret_env`` (dealerflow keeps secrets in the environment only). HTML scraping is
deliberately not implemented, and every feed source must record who authorised it.
"""

from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import urlparse

from ...audit import track_integration
from ...config import get_settings
from ...integrations.errors import NotConfiguredError, PermanentError
from ...integrations.http import request
from ...models_dealer import ImportRun, InventorySource
from ...tenancy import ClientContext
from ..dealer_errors import BadRequest
from ..netguard import assert_public_url
from .importer import run_import
from .parsers import parse_csv, parse_json_feed, parse_xlsx

FEED_TYPES = ("feed_api", "website_feed", "dms_export")


def validate_feed_config(source_type: str, config: dict[str, Any], authorization_note: str | None,
                         secret_env: str | None = None) -> None:
    if secret_env and not secret_env.startswith("DF_FEED_"):
        raise BadRequest("Feed credential variables must be named DF_FEED_<NAME> (set them in the server environment)")
    if source_type not in FEED_TYPES or not config.get("url"):
        return
    if urlparse(config["url"]).scheme != "https":
        raise BadRequest("Feed URLs must use https")
    assert_public_url(config["url"])
    if not (authorization_note or "").strip():
        raise BadRequest("Record who at the dealership authorised this feed before enabling it")
    if config.get("format", "csv") not in ("csv", "json", "xlsx"):
        raise BadRequest("Feed format must be csv, json or xlsx")


def _auth_headers(source: InventorySource) -> dict[str, str]:
    if not source.secret_env:
        return {}
    secret = os.environ.get(source.secret_env, "")
    if not secret:
        raise NotConfiguredError(f"Environment variable {source.secret_env} is not set")
    kind = (source.config or {}).get("auth", {}).get("type", "bearer")
    if kind == "header":
        return {(source.config or {})["auth"].get("header_name", "X-API-Key"): secret}
    if kind == "basic":
        return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}
    return {"Authorization": f"Bearer {secret}"}


def fetch_feed(db, source: InventorySource) -> tuple[bytes, str]:
    cfg = source.config or {}
    url = cfg.get("url")
    if not url:
        raise BadRequest("Source has no feed URL configured")
    assert_public_url(url)  # checked on every fetch: DNS can change after the source was saved
    s = get_settings()
    with track_integration(db, "inventory_feed", "fetch") as log:
        resp = request("GET", url, provider="inventory_feed", headers=_auth_headers(source), retries=2, rate=(2, 1.0),
                       timeout=s.feed_timeout_seconds, follow_redirects=False)
        if len(resp.content) > s.max_feed_bytes:
            raise PermanentError(f"inventory_feed: feed exceeds {s.max_feed_bytes} bytes")
        log["detail"] = f"source {source.id}: {len(resp.content)} bytes"
    return resp.content, cfg.get("format", "csv")


def sync_feed_source(ctx: ClientContext, source: InventorySource, trigger: str = "schedule") -> ImportRun:
    data, fmt = fetch_feed(ctx.db, source)
    cfg = source.config or {}
    if fmt == "json":
        headers, rows = parse_json_feed(data, cfg.get("records_path"))
    elif fmt == "xlsx":
        headers, rows = parse_xlsx(data, cfg.get("sheet"))
    else:
        headers, rows = parse_csv(data)
    return run_import(ctx, source, headers, rows, trigger=trigger, file_name=urlparse(cfg["url"]).path.rsplit("/", 1)[-1],
                      file_bytes=data)
