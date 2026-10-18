"""Authorised inventory feed retrieval (feed_api, website_feed, dms_export via URL).

Only structured feeds the dealership (or its DMS/website/syndication provider)
has authorised us to read are supported: CSV, JSON or XLSX over HTTPS, with an
optional credential the dealer issued. HTML page scraping is deliberately not
implemented. Every feed source must carry an ``authorization_note``.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.core.crypto import decrypt_secret
from app.core.errors import BadRequest
from app.core.netguard import assert_public_url
from app.core.tenancy import ClientContext
from app.inventory.importer import run_import
from app.inventory.parsers import parse_csv, parse_json_feed, parse_xlsx
from app.models import ImportRun, InventorySource

FEED_TYPES = ("feed_api", "website_feed", "dms_export")
_transport: httpx.BaseTransport | None = None  # tests inject a MockTransport


def set_transport(t: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = t


def validate_feed_config(source_type: str, config: dict[str, Any], authorization_note: str | None) -> None:
    if source_type not in FEED_TYPES or not config.get("url"):
        return
    if urlparse(config["url"]).scheme != "https":
        raise BadRequest("Feed URLs must use https")
    assert_public_url(config["url"])
    if not (authorization_note or "").strip():
        raise BadRequest("Record who at the dealership authorised this feed (authorization_note) before enabling it")
    if config.get("format", "csv") not in ("csv", "json", "xlsx"):
        raise BadRequest("Feed format must be csv, json or xlsx")


def fetch_feed(source: InventorySource) -> tuple[bytes, str]:
    cfg = source.config or {}
    url = cfg.get("url")
    if not url:
        raise BadRequest("Source has no feed URL configured")
    if _transport is None:  # tests inject a mock transport; real fetches are checked every time (DNS can change)
        assert_public_url(url)
    headers = {"User-Agent": "CarDealerFuture-InventorySync/0.2 (+authorized dealer feed)"}
    auth = cfg.get("auth") or {}
    if source.secret_ciphertext:
        secret = decrypt_secret(source.secret_ciphertext)
        kind = auth.get("type", "bearer")
        if kind == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif kind == "header":
            headers[auth.get("header_name", "X-API-Key")] = secret
        elif kind == "basic":
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
    with httpx.Client(timeout=settings.feed_timeout_seconds, transport=_transport, follow_redirects=False) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"Feed returned HTTP {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > settings.max_feed_bytes:
                    raise RuntimeError(f"Feed exceeds {settings.max_feed_bytes} bytes")
                chunks.append(chunk)
    return b"".join(chunks), cfg.get("format", "csv")


def sync_feed_source(ctx: ClientContext, source: InventorySource, trigger: str = "schedule") -> ImportRun:
    data, fmt = fetch_feed(source)
    cfg = source.config or {}
    if fmt == "json":
        headers, rows = parse_json_feed(data, cfg.get("records_path"))
    elif fmt == "xlsx":
        headers, rows = parse_xlsx(data, cfg.get("sheet"))
    else:
        headers, rows = parse_csv(data)
    return run_import(ctx, source, headers, rows, trigger=trigger, file_name=urlparse(cfg["url"]).path.rsplit("/", 1)[-1],
                      file_bytes=data)
