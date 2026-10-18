from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.core.errors import BadRequest, Unprocessable
from app.listings import service
from app.listings.dealer_settings import update_settings
from app.listings.guardrails import warning_key
from app.models import ChannelPublication, Job, PublishOperation
from app.publishing import service as pub
from app.publishing.adapters import meta_catalog
from tests.conftest import run_worker
from tests.helpers import configure_dealer, import_csv, listing_for, vehicle_by_stock
from tests.test_listings import ctx_admin

FB_URL = "https://www.facebook.com/marketplace/item/1234567890123/"


def approved_listing(world, channels=("facebook_marketplace",), stock="B200"):
    ctx, _, src = import_csv(world["a"].id)
    configure_dealer(ctx, enabled_channels=list(channels))
    v = vehicle_by_stock(ctx, stock)
    lst = listing_for(ctx, v)
    rev = service.generate_listing(ctx, lst.id)
    service.approve_listing(ctx_admin(ctx), lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    ctx.db.commit()
    return ctx, src, v, lst


def pubs(ctx, lst, channel):
    return ctx.db.scalar(select(ChannelPublication).where(ChannelPublication.listing_id == lst.id, ChannelPublication.channel == channel))


def test_marketplace_is_manual_and_requires_evidence(world):
    ctx, _, v, lst = approved_listing(world)
    p = pubs(ctx, lst, "facebook_marketplace")
    assert p.status == "manual_required"  # never auto-marked published
    view = pub.publication_view(ctx, p, with_package=True)
    assert view["mode"] == "manual"
    pkg = view["package"]
    assert pkg["fields"]["price"] == 18995 and pkg["copy"]["es"]["title"]
    assert pkg["photos"] == ["https://img.example/b200.jpg"] and len(pkg["checklist"]) >= 5
    admin = ctx_admin(ctx)
    with pytest.raises(BadRequest):
        pub.record_manual_publication(admin, p.id, url=FB_URL, attested=False)
    with pytest.raises(BadRequest):
        pub.record_manual_publication(admin, p.id, url="https://example.com/listing", attested=True)
    pub.record_manual_publication(admin, p.id, url=FB_URL, attested=True)
    assert p.status == "published_confirmed" and p.confirmation_type == "manual_attested"
    # request_publish on a manual channel only re-prepares the package; nothing is sent anywhere
    assert pub.request_publish(admin, p.id).operation == "prepare_manual"


def test_price_change_flags_live_listing_and_manual_update_recorded(world):
    ctx, src, v, lst = approved_listing(world)
    p = pubs(ctx, lst, "facebook_marketplace")
    pub.record_manual_publication(ctx_admin(ctx), p.id, url=FB_URL, attested=True)
    ctx.db.commit()
    from tests.conftest import SAMPLE_CSV
    import_csv(world["a"].id, SAMPLE_CSV.replace("18995", "17995"), source=src)
    ctx.db.expire_all()
    p = pubs(ctx, lst, "facebook_marketplace")
    assert p.needs_update and "price" in p.update_reason
    assert listing_for(ctx, v).status == "awaiting_approval" and listing_for(ctx, v).content_outdated
    run_worker()  # regenerates the draft
    ctx.db.expire_all()
    lst = listing_for(ctx, v)
    rev = service.current_revision(ctx, lst)
    assert "$17,995" in rev.content["en"]["description"]
    with pytest.raises(Exception):
        pub.record_manual_update(ctx_admin(ctx), p.id, attested=True)  # not approved yet
    service.approve_listing(ctx_admin(ctx), lst.id, rev.id, [warning_key(w) for w in rev.guardrails["warnings"]])
    pub.record_manual_update(ctx_admin(ctx), p.id, attested=True)
    assert not p.needs_update


def test_sold_vehicle_requires_withdrawal(world):
    ctx, src, v, lst = approved_listing(world)
    p = pubs(ctx, lst, "facebook_marketplace")
    pub.record_manual_publication(ctx_admin(ctx), p.id, url=FB_URL, attested=True)
    ctx.db.commit()
    from tests.conftest import SAMPLE_CSV
    lines = SAMPLE_CSV.strip().splitlines()
    csv = "\n".join([lines[0] + ",Status", lines[1] + ",Available", lines[2] + ",Sold", lines[3] + ",Available"]) + "\n"
    import_csv(world["a"].id, csv, source=src)
    ctx.db.expire_all()
    p = pubs(ctx, lst, "facebook_marketplace")
    assert p.status == "withdrawal_required"
    assert listing_for(ctx, v).status == "sold_withdrawn"
    with pytest.raises(Exception):
        pub.record_manual_publication(ctx_admin(ctx), p.id, url=FB_URL, attested=True)
    pub.record_manual_removal(ctx_admin(ctx), p.id, attested=True)
    assert p.status == "withdrawn"


def test_api_channel_submit_confirm_and_idempotency(world):
    ctx, _, v, lst = approved_listing(world, channels=("sandbox",))
    p = pubs(ctx, lst, "sandbox")
    assert p.status == "ready_to_publish"  # approval alone does not publish
    op1 = pub.request_publish(ctx_admin(ctx), p.id)
    op2 = pub.request_publish(ctx_admin(ctx), p.id)
    assert op1.id == op2.id  # repeated clicks -> same operation
    ctx.db.commit()
    run_worker()
    ctx.db.expire_all()
    p = pubs(ctx, lst, "sandbox")
    assert p.status == "published_confirmed" and p.confirmation_type == "api"
    ops = ctx.db.scalars(select(PublishOperation).where(PublishOperation.publication_id == p.id)).all()
    assert [o.operation for o in ops] == ["create", "status_check"]
    assert ctx.db.scalar(select(Job).where(Job.type == "publishing.execute", Job.status != "succeeded")) is None


def test_api_channel_retries_then_succeeds(world):
    ctx, _, v, lst = approved_listing(world, channels=("sandbox",))
    pub.upsert_connection(ctx_admin(ctx), "sandbox", config={"fail_next": 2, "fail_mode": "retryable"})
    p = pubs(ctx, lst, "sandbox")
    pub.request_publish(ctx_admin(ctx), p.id)
    ctx.db.commit()
    from datetime import timedelta
    from app.core.time import utcnow
    for i in range(4):  # advance time past the backoff each round
        run_worker(now=utcnow() + timedelta(hours=2 * (i + 1)))
    ctx.db.expire_all()
    p = pubs(ctx, lst, "sandbox")
    assert p.status == "published_confirmed"
    op = ctx.db.scalar(select(PublishOperation).where(PublishOperation.publication_id == p.id, PublishOperation.operation == "create"))
    assert op.attempt_count == 3


def test_permanent_failure_marks_failed_and_manual_retry(world):
    ctx, _, v, lst = approved_listing(world, channels=("sandbox",))
    pub.upsert_connection(ctx_admin(ctx), "sandbox", config={"fail_next": 1, "fail_mode": "permanent"})
    p = pubs(ctx, lst, "sandbox")
    pub.request_publish(ctx_admin(ctx), p.id)
    ctx.db.commit()
    run_worker()
    ctx.db.expire_all()
    p = pubs(ctx, lst, "sandbox")
    assert p.status == "failed" and p.failure_count == 1 and "Simulated" in p.last_error
    pub.retry_publication(ctx_admin(ctx), p.id)
    ctx.db.commit()
    run_worker()
    ctx.db.expire_all()
    assert pubs(ctx, lst, "sandbox").status == "published_confirmed"


def test_sold_vehicle_deleted_from_api_channel(world):
    ctx, src, v, lst = approved_listing(world, channels=("sandbox",))
    pub.request_publish(ctx_admin(ctx), pubs(ctx, lst, "sandbox").id)
    ctx.db.commit()
    run_worker()
    from app.api.inventory import set_vehicle_status, StatusIn
    ctx.db.expire_all()
    set_vehicle_status(v.id, StatusIn(status="sold"), ctx_admin(ctx))
    ctx.db.commit()
    run_worker()
    ctx.db.expire_all()
    assert pubs(ctx, lst, "sandbox").status == "withdrawn"


def test_cannot_publish_unavailable_or_unapproved(world):
    ctx, _, v, lst = approved_listing(world, channels=("sandbox",))
    v.stale = True
    with pytest.raises(Unprocessable):
        pub.request_publish(ctx_admin(ctx), pubs(ctx, lst, "sandbox").id)


def test_meta_catalog_adapter_guarded_and_request_shape(world, monkeypatch):
    from app.config import settings
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path.endswith("/items_batch"):
            return httpx.Response(200, json={"handles": ["h-123"]})
        return httpx.Response(200, json={"data": [{"status": "finished", "errors": []}]})
    meta_catalog.set_transport(httpx.MockTransport(handler))
    try:
        ctx, _, v, lst = approved_listing(world, channels=("meta_catalog",))
        v.location_address = "123 Main St"
        admin = ctx_admin(ctx)
        pub.upsert_connection(admin, "meta_catalog", config={"catalog_id": "999", "vdp_url_template": "https://alpha.test/inv/{stock_number}"},
                              secret="system-user-token")
        p = pubs(ctx, lst, "meta_catalog")
        pub.request_publish(admin, p.id)
        ctx.db.commit()
        run_worker()
        ctx.db.expire_all()
        p = pubs(ctx, lst, "meta_catalog")
        assert p.status == "failed" and "disabled" in p.last_error  # global switch off -> nothing sent
        assert calls == []
        monkeypatch.setattr(settings, "meta_live_publishing_allowed", True)
        pub.upsert_connection(admin, "meta_catalog", config={"catalog_id": "999", "vdp_url_template": "https://alpha.test/inv/{stock_number}"},
                              live_publishing=True)
        pub.retry_publication(admin, p.id)
        ctx.db.commit()
        run_worker()
        ctx.db.expire_all()
        p = pubs(ctx, lst, "meta_catalog")
        assert p.status == "published_confirmed" and p.confirmation_type == "api"
        batch = calls[0]
        assert batch.url.path == "/v24.0/999/items_batch"
        form = dict(x.split("=", 1) for x in batch.content.decode().split("&"))
        from urllib.parse import unquote_plus
        reqs = json.loads(unquote_plus(form["requests"]))
        assert unquote_plus(form["item_type"]) == "VEHICLE" and reqs[0]["method"] == "UPDATE"
        data = reqs[0]["data"]
        assert data["price"] == "18995 USD" and data["mileage"] == {"value": 45000, "unit": "MI"} and data["url"].endswith("/B200")
        assert "check_batch_request_status" in calls[1].url.path
    finally:
        meta_catalog.set_transport(None)
