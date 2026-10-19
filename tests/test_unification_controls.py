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


def _fake_anthropic(monkeypatch, calls: list, parsed=None):
    """Stand-in for the Anthropic SDK client (no network); records every request that reaches it."""
    from types import SimpleNamespace

    import anthropic

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(id="msg_u1", model="claude-opus-5-5", stop_reason="end_turn", parsed_output=parsed,
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0))

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=SimpleNamespace(parse=parse)))


def _enable_listing_ai(monkeypatch):
    from dealerflow.config import get_settings

    monkeypatch.setenv("DF_LISTING_AI_ENABLED", "true")
    monkeypatch.setenv("DF_AI_ENABLED", "true")
    monkeypatch.setenv("DF_ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()


def test_emergency_stop_blocks_ai_listing_copy_and_falls_back_to_template(monkeypatch):
    _enable_listing_ai(monkeypatch)
    calls: list = []
    _fake_anthropic(monkeypatch, calls)
    cid = make_client()
    ctx, _, _ = import_csv(cid)
    configure_dealer(ctx)
    _emergency_stop()
    rev = service.generate_listing(ctx, listing_for(ctx, vehicle_by_stock(ctx, "B200")).id)
    assert calls == [] and rev.origin == "template_generator"  # the request never reached the provider
    assert any("unavailable" in u for u in rev.uncertainty)
    ctx.db.commit()
    assert _blocked("anthropic")
    ctx.close()


def test_ai_call_is_logged_through_the_gate(monkeypatch):
    from dealerflow.services.listings import ai_generator

    _enable_listing_ai(monkeypatch)
    lc = ai_generator.LangCopy(title="2019 Toyota Camry SE", description="Clean sedan.", call_to_action="Call us.",
                               search_terms=["toyota camry"])
    calls: list = []
    _fake_anthropic(monkeypatch, calls, parsed=ai_generator.ListingCopy(en=lc, es=lc, uncertainty=[]))
    cid = make_client()
    ctx, _, _ = import_csv(cid)
    configure_dealer(ctx)
    rev = service.generate_listing(ctx, listing_for(ctx, vehicle_by_stock(ctx, "B200")).id)
    ctx.db.commit()
    assert len(calls) == 1 and rev.origin == "ai_generator"
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
    other = make_client(name="Beta Auto")
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
