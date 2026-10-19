"""Regression tests: hardened Phase 1.5 controls apply to the Phase 2 code paths after unification."""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import select

from dealerflow.db import session_scope
from dealerflow.integrations.listing_channels import MockListingChannel
from dealerflow.models import AgentState, IntegrationLog, User

from dealerflow.services import netguard
from dealerflow.services.listings import service
from tests.dealer_helpers import (PASSWORD, configure_dealer, dealer_login, drain, import_csv, listing_for,
                                  make_client, make_dealer_user, vehicle_by_stock)
from tests.test_dealer_publishing import approved, get_pub


def _emergency_stop():
    with session_scope() as db:
        st = db.get(AgentState, 1) or AgentState(id=1)
        st.status = "emergency_stopped"
        db.add(st)


def _blocked(provider: str) -> list[IntegrationLog]:
    with session_scope() as db:
        return list(db.scalars(select(IntegrationLog).where(IntegrationLog.provider == provider,
                                                            IntegrationLog.status == "blocked")))


# --- Global emergency stop covers Phase 2 external actions ------------------------------------

def test_emergency_stop_blocks_listing_publication():
    cid, owner, oc, lst = approved(channels=("mock",))
    MockListingChannel.calls = []  # class-level record shared across tests
    _emergency_stop()
    from dealerflow.services.publishing import service as pub

    pub.request_publish(oc, get_pub(oc, lst, "mock").id)
    oc.db.commit()
    drain()
    assert MockListingChannel.calls == []
    p = get_pub(oc, lst, "mock")
    assert p.status != "published_confirmed"
    oc.close()


def test_emergency_stop_blocks_ai_listing_copy_and_falls_back_to_template(monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.services.listings import ai_generator

    monkeypatch.setenv("DF_LISTING_AI_ENABLED", "true")
    monkeypatch.setenv("DF_ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    calls = []
    monkeypatch.setattr(ai_generator, "generate", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError))
    cid = make_client()
    ctx, _, _ = import_csv(cid)
    configure_dealer(ctx)
    _emergency_stop()
    rev = service.generate_listing(ctx, listing_for(ctx, vehicle_by_stock(ctx, "B200")).id)
    assert calls == [] and rev.origin == "template_generator"
    assert any("unavailable" in u for u in rev.uncertainty)
    ctx.db.commit()
    assert _blocked("anthropic")
    ctx.close()


def test_ai_call_is_logged_through_the_gate(monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.integrations import llm
    from dealerflow.services.listings import ai_generator

    monkeypatch.setenv("DF_LISTING_AI_ENABLED", "true")
    monkeypatch.setenv("DF_ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    lc = ai_generator.LangCopy(title="2019 Toyota Camry SE", description="Clean sedan.", call_to_action="Call us.",
                               search_terms=["toyota camry"])
    monkeypatch.setattr(ai_generator, "structured_call", lambda *a, **k: llm.LLMResult(
        parsed=ai_generator.ListingCopy(en=lc, es=lc, uncertainty=[]), model="claude-opus-5-5", input_tokens=1, output_tokens=1))
    cid = make_client()
    ctx, _, _ = import_csv(cid)
    configure_dealer(ctx)
    service.generate_listing(ctx, listing_for(ctx, vehicle_by_stock(ctx, "B200")).id)
    ctx.db.commit()
    with session_scope() as db:
        assert db.scalar(select(IntegrationLog).where(IntegrationLog.provider == "anthropic", IntegrationLog.status == "ok"))
    ctx.close()


# --- One SSRF guard, strictest rules ------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://127.0.0.1/feed.csv", "https://10.1.2.3/f", "https://169.254.169.254/latest",
                                 "https://100.64.0.1/feed", "https://[::1]/f", "https://localhost/f", "ftp://x.com/f",
                                 "https://host.invalid/f"])
def test_shared_ssrf_guard_rejects_non_public_targets(url):
    with pytest.raises(netguard.UnsafeURL):
        netguard.assert_public_url(url)


def test_shared_ssrf_guard_fails_closed_on_dns_error(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(netguard, "_resolve", boom)
    with pytest.raises(netguard.UnsafeURL, match="DNS"):
        netguard.assert_public_url("https://feeds.dealer-site.com/inventory.csv")


def test_shared_ssrf_guard_requires_https_for_feeds_but_not_websites(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 80))])
    with pytest.raises(netguard.UnsafeURL, match="https"):
        netguard.assert_public_url("http://dealer-site.com/feed.csv")
    netguard.assert_public_url("http://dealer-site.com/", require_https=False)
    from dealerflow.integrations import website
    website.assert_public_url("http://dealer-site.com/")  # dealer websites use the same guard


def test_feed_fetch_uses_the_shared_guard():
    from dealerflow.services.inventory import feeds

    assert feeds.assert_public_url is netguard.assert_public_url


# --- Session controls apply to dealership users too ------------------------------------------

def test_password_change_ends_dealer_user_sessions(client):
    from dealerflow.security import hash_password

    cid = make_client()
    uid = make_dealer_user(cid, "owner@alpha.example")
    dealer_login(client, "owner@alpha.example", PASSWORD)
    assert client.get(f"/dealers/{cid}", follow_redirects=False).status_code == 200
    with session_scope() as db:
        db.get(User, uid).password_hash = hash_password("another strong password")
    assert client.get(f"/dealers/{cid}", follow_redirects=False).status_code == 303


def test_dealer_user_still_confined_to_own_workspace(client):
    cid = make_client()
    other = make_client(name="Beta Auto", slug="beta-auto")
    make_dealer_user(cid, "owner@alpha.example")
    dealer_login(client, "owner@alpha.example", PASSWORD)
    assert client.get("/prospects", follow_redirects=False).status_code == 403
    assert client.get("/agent", follow_redirects=False).status_code == 403
    assert client.get(f"/dealers/{other}", follow_redirects=False).status_code in (403, 404)




@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_csrf_covers_every_unsafe_method(method):
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from dealerflow.web.deps import check_csrf

    scope = {"type": "http", "method": method, "path": "/x", "headers": [(b"content-type", b"application/json")],
             "session": {"csrf": "right"}, "query_string": b""}
    with pytest.raises(HTTPException):
        asyncio.run(check_csrf(Request(scope)))
    ok = dict(scope, headers=[(b"x-csrf-token", b"right")])
    asyncio.run(check_csrf(Request(ok)))
    bearer = dict(scope, headers=[(b"authorization", b"Bearer df_x")])
    asyncio.run(check_csrf(Request(bearer)))  # API keys are exempt


def test_secrets_can_come_from_files_not_the_environment(tmp_path, monkeypatch):
    from dealerflow.config import get_settings

    (tmp_path / "df_smtp_password").write_text("from-a-secret-file")
    monkeypatch.setenv("DF_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("DF_SMTP_PASSWORD", raising=False)
    get_settings.cache_clear()
    assert get_settings().smtp_password.get_secret_value() == "from-a-secret-file"


# --- Phase 4 weaknesses fixed at merge ----------------------------------------------------------

def test_seo_crawler_refuses_redirect_to_internal_address(monkeypatch):
    import httpx

    from dealerflow.services import seo_crawler

    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [(0, 0, 0, "", ("10.0.0.7" if host == "internal.dealer-site.com"
                                                                                else "93.184.216.34", port))])
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "dealer-site.com":
            return httpx.Response(302, headers={"location": "http://internal.dealer-site.com/admin"})
        return httpx.Response(200, text="<html>secret</html>")

    c = seo_crawler.Crawler("https://dealer-site.com", max_pages=3, delay_seconds=0.5,
                            client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    assert c._get("https://dealer-site.com/") is None
    assert not any("internal" in u for u in seen)


def test_emergency_stop_blocks_seo_crawl(db):
    from dealerflow.integrations.errors import ExternalActionsHalted
    from dealerflow.models import SeoAudit, SeoSite
    from dealerflow.services import seo_audit

    cid = make_client()
    site = SeoSite(client_id=cid, base_url="https://alpha.example", authorized=True, authorization_note="test")
    db.add(site)
    db.flush()
    a = SeoAudit(client_id=cid, site_id=site.id, status="queued")
    db.add(a)
    db.commit()
    _emergency_stop()
    calls = []
    monkeypatch_client = __import__("httpx").Client(transport=__import__("httpx").MockTransport(
        lambda r: calls.append(r) or __import__("httpx").Response(200, text="<html></html>")))
    with pytest.raises(ExternalActionsHalted):
        seo_audit.run_audit(db, db.get(SeoAudit, a.id), http_client=monkeypatch_client, sleep=lambda s: None)
    assert calls == []


def test_logs_redact_emails_and_tokens():
    import logging

    from dealerflow.logging_setup import JsonFormatter

    rec = logging.makeLogRecord({"msg": "sent to owner@dealer-site.com", "name": "t", "levelname": "INFO",
                                 "detail": "Authorization: Bearer abc.def.ghi failed for 281-555-0199"})
    out = JsonFormatter().format(rec)
    assert "owner@dealer-site.com" not in out and "abc.def.ghi" not in out and "555-0199" not in out


def test_worker_refuses_insecure_production_config(monkeypatch):
    from dealerflow import worker
    from dealerflow.config import get_settings

    monkeypatch.setenv("DF_ENV", "production")
    monkeypatch.setenv("DF_SECRET_KEY", "short")
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="Refusing to start the worker"):
        worker.main(poll_seconds=0)


def test_api_rate_limiter_memory_is_bounded():
    from dealerflow.web.app import _ApiRateLimiter

    lim = _ApiRateLimiter()
    for i in range(12_000):
        lim.allow(f"ip{i}", 5, window=0.0)
    assert len(lim.hits) <= 10_001

