"""Phase 2 publishing: Marketplace manual workflow, simulated API channel retries/idempotency, Meta catalog adapter."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from dealerflow.db import session_scope
from dealerflow.integrations import http as http_mod
from dealerflow.integrations.listing_channels import MockListingChannel
from dealerflow.models import IntegrationLog
from dealerflow.models_dealer import ChannelPublication, PublishOperation
from dealerflow.services.dealer_errors import BadRequest, Unprocessable
from dealerflow.services.listings import service
from dealerflow.services.listings.guardrails import warning_key
from dealerflow.services.publishing import service as pub
from tests.dealer_helpers import (SAMPLE_CSV, configure_dealer, ctx_as, drain, import_csv, listing_for, make_client,
                                  make_dealer_user, vehicle_by_stock)

FB_URL = "https://www.facebook.com/marketplace/item/1234567890123/"
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def _reset_mock():
    MockListingChannel.reset()
    yield
    MockListingChannel.reset()


def approved(channels=("facebook_marketplace",), stock="B200"):
    cid = make_client()
    ctx, _, _ = import_csv(cid)
    configure_dealer(ctx, enabled_channels=list(channels))
    ctx.close()
    owner = make_dealer_user(cid, "owner@alpha.example")
    oc = ctx_as(cid, owner)
    lst = listing_for(oc, vehicle_by_stock(oc, stock))
    rev = service.generate_listing(oc, lst.id)
    service.approve_listing(oc, lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    oc.db.commit()
    return cid, owner, oc, lst


def get_pub(ctx, lst, channel):
    ctx.db.expire_all()
    return ctx.db.scalar(select(ChannelPublication).where(ChannelPublication.listing_id == lst.id, ChannelPublication.channel == channel))


def test_marketplace_is_manual_and_requires_evidence():
    cid, owner, oc, lst = approved()
    p = get_pub(oc, lst, "facebook_marketplace")
    assert p.status == "manual_required"  # approval never marks anything published
    view = pub.publication_view(oc, p, with_package=True)
    pkg = view["package"]
    assert view["mode"] == "manual" and pkg["fields"]["price"] == 18995 and pkg["copy"]["es"]["title"]
    assert pkg["photos"] == ["https://img.example/b200.jpg"] and len(pkg["checklist"]) >= 5
    with pytest.raises(BadRequest):
        pub.record_manual_publication(oc, p.id, url=FB_URL, attested=False)
    with pytest.raises(BadRequest):
        pub.record_manual_publication(oc, p.id, url="https://example.com/listing", attested=True)
    pub.record_manual_publication(oc, p.id, url=FB_URL, attested=True)
    assert p.status == "published_confirmed" and p.confirmation_type == "manual_attested"
    assert pub.request_publish(oc, p.id).operation == "prepare_manual"  # nothing is sent anywhere
    oc.close()


def test_price_change_flags_live_listing_and_manual_update_recorded():
    cid, owner, oc, lst = approved()
    pub.record_manual_publication(oc, get_pub(oc, lst, "facebook_marketplace").id, url=FB_URL, attested=True)
    oc.db.commit()
    import_csv(cid, SAMPLE_CSV.replace("18995", "17995"))[0].close()
    p = get_pub(oc, lst, "facebook_marketplace")
    assert p.needs_update and "price" in p.update_reason
    drain()  # regenerates the draft
    oc.db.expire_all()
    lst = service.get_listing(oc, lst.id)
    assert lst.status == "awaiting_approval"
    rev = service.current_revision(oc, lst)
    assert "$17,995" in rev.content["en"]["description"]
    with pytest.raises(Exception):
        pub.record_manual_update(oc, p.id, attested=True)  # new revision not approved yet
    service.approve_listing(oc, lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    pub.record_manual_update(oc, p.id, attested=True)
    assert not p.needs_update
    oc.close()


def test_sold_vehicle_requires_withdrawal():
    cid, owner, oc, lst = approved()
    pub.record_manual_publication(oc, get_pub(oc, lst, "facebook_marketplace").id, url=FB_URL, attested=True)
    oc.db.commit()
    lines = SAMPLE_CSV.strip().splitlines()
    import_csv(cid, "\n".join([lines[0] + ",Status", lines[1] + ",Available", lines[2] + ",Sold", lines[3] + ",Available"]) + "\n")[0].close()
    p = get_pub(oc, lst, "facebook_marketplace")
    assert p.status == "withdrawal_required" and service.get_listing(oc, lst.id).status == "sold_withdrawn"
    with pytest.raises(Exception):
        pub.record_manual_publication(oc, p.id, url=FB_URL, attested=True)
    pub.record_manual_removal(oc, p.id, attested=True)
    assert p.status == "withdrawn"
    oc.close()


def test_api_channel_submit_confirm_and_idempotency():
    cid, owner, oc, lst = approved(channels=("mock",))
    p = get_pub(oc, lst, "mock")
    assert p.status == "ready_to_publish"
    op1, op2 = pub.request_publish(oc, p.id), pub.request_publish(oc, p.id)
    assert op1.id == op2.id  # repeated clicks -> one operation
    oc.db.commit()
    drain()
    p = get_pub(oc, lst, "mock")
    assert p.status == "published_confirmed" and p.confirmation_type == "api"
    ops = oc.db.scalars(select(PublishOperation).where(PublishOperation.publication_id == p.id)).all()
    assert [o.operation for o in ops] == ["create", "status_check"] and MockListingChannel.calls == ["publish", "status"]
    with session_scope() as db:
        assert db.scalar(select(IntegrationLog).where(IntegrationLog.provider == "mock_listing_channel")).simulated
    oc.close()


def test_api_channel_retries_then_succeeds():
    cid, owner, oc, lst = approved(channels=("mock",))
    MockListingChannel.fail_next = 2
    pub.request_publish(oc, get_pub(oc, lst, "mock").id)
    oc.db.commit()
    drain()
    p = get_pub(oc, lst, "mock")
    assert p.status == "published_confirmed"
    op = oc.db.scalar(select(PublishOperation).where(PublishOperation.publication_id == p.id, PublishOperation.operation == "create"))
    assert op.attempt_count == 3
    oc.close()


def test_permanent_failure_marks_failed_and_manual_retry():
    cid, owner, oc, lst = approved(channels=("mock",))
    MockListingChannel.fail_next_permanent = 1
    pub.request_publish(oc, get_pub(oc, lst, "mock").id)
    oc.db.commit()
    drain()
    p = get_pub(oc, lst, "mock")
    assert p.status == "failed" and p.failure_count == 1 and "permanent" in p.last_error
    pub.retry_publication(oc, p.id)
    oc.db.commit()
    drain()
    assert get_pub(oc, lst, "mock").status == "published_confirmed"
    oc.close()


def test_retries_are_bounded(monkeypatch):
    monkeypatch.setenv("DF_PUBLISH_MAX_ATTEMPTS", "3")
    from dealerflow.config import get_settings
    get_settings.cache_clear()
    cid, owner, oc, lst = approved(channels=("mock",))
    MockListingChannel.fail_next = 99
    pub.request_publish(oc, get_pub(oc, lst, "mock").id)
    oc.db.commit()
    drain(rounds=10)
    p = get_pub(oc, lst, "mock")
    op = oc.db.scalar(select(PublishOperation).where(PublishOperation.publication_id == p.id))
    assert op.status == "dead" and op.attempt_count == 3 and p.status == "failed"
    oc.close()


def test_sold_vehicle_deleted_from_api_channel(client):
    cid, owner, oc, lst = approved(channels=("mock",))
    pub.request_publish(oc, get_pub(oc, lst, "mock").id)
    oc.db.commit()
    drain()
    from tests.dealer_helpers import dealer_login, page_csrf
    dealer_login(client, "owner@alpha.example")
    v = vehicle_by_stock(oc, "B200")
    csrf = page_csrf(client, f"/dealers/{cid}/vehicles/{v.id}")
    r = client.post(f"/dealers/{cid}/vehicles/{v.id}/status", data={"csrf": csrf, "status": "sold"}, follow_redirects=False)
    assert r.status_code == 303
    drain()
    assert get_pub(oc, lst, "mock").status == "withdrawn" and "delete" in MockListingChannel.calls
    oc.close()


def test_cannot_publish_stale_vehicle():
    cid, owner, oc, lst = approved(channels=("mock",))
    vehicle_by_stock(oc, "B200").stale = True
    oc.db.flush()
    with pytest.raises(Unprocessable):
        pub.request_publish(oc, get_pub(oc, lst, "mock").id)
    oc.close()


def test_meta_catalog_guarded_and_request_shape(monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path.endswith("/items_batch"):
            return httpx.Response(200, json={"handles": ["h-123"]})
        return httpx.Response(200, json={"data": [{"status": "finished", "errors": []}]})
    monkeypatch.setattr(http_mod.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("DF_META_TOKEN_ALPHA", "system-user-token")
    cid, owner, oc, lst = approved(channels=("meta_catalog",))
    vehicle_by_stock(oc, "B200").location_address = "123 Main St"
    cfg = {"catalog_id": "999", "vdp_url_template": "https://alpha.example/inv/{stock_number}"}
    with pytest.raises(BadRequest):
        pub.upsert_connection(oc, "meta_catalog", config=cfg, token_env="DF_SECRET_KEY")  # only DF_META_TOKEN_* names
    pub.upsert_connection(oc, "meta_catalog", config=cfg, token_env="DF_META_TOKEN_ALPHA")
    p = get_pub(oc, lst, "meta_catalog")
    pub.request_publish(oc, p.id)
    oc.db.commit()
    drain()
    p = get_pub(oc, lst, "meta_catalog")
    assert p.status == "failed" and "disabled" in p.last_error and calls == []  # global switch off: nothing sent
    monkeypatch.setenv("DF_META_LIVE_PUBLISHING_ALLOWED", "true")
    from dealerflow.config import get_settings
    get_settings.cache_clear()
    pub.upsert_connection(oc, "meta_catalog", config=cfg, live_publishing=True)
    pub.retry_publication(oc, p.id)
    oc.db.commit()
    drain()
    p = get_pub(oc, lst, "meta_catalog")
    assert p.status == "published_confirmed" and p.confirmation_type == "api"
    batch = calls[0]
    assert batch.url.path == "/v24.0/999/items_batch"
    form = parse_qs(batch.content.decode())
    reqs = json.loads(form["requests"][0])
    assert form["item_type"] == ["VEHICLE"] and reqs[0]["method"] == "UPDATE"
    data = reqs[0]["data"]
    assert data["price"] == "18995 USD" and data["mileage"] == {"value": 45000, "unit": "MI"} and data["url"].endswith("/B200")
    assert "check_batch_request_status" in calls[1].url.path
    oc.close()
