"""SEO audit service, onboarding gating and client reports. The 'website' is an in-memory httpx transport."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from dealerflow.models import SeoAudit, SeoIssue, SeoSite, User
from dealerflow.services import client_reports, onboarding, seo_audit
from dealerflow.services.seo_crawler import Crawler
from tests.phase4 import two_clients

HOME_V1 = """<html><head><title>Alpha</title></head><body>
<h1>Alpha Motors</h1><h1>Used cars</h1><p>Call 281-555-0101</p>
<a href="/inventory/2019-honda-civic">Civic</a> <a href="/missing">Old page</a> <a href="/private/x">Private</a>
<img src="/a.jpg"></body></html>"""
HOME_V2 = """<html lang="en"><head><title>Used Cars in Sugar Land, TX | Alpha Motors</title>
<meta name="description" content="Quality pre-owned vehicles in Sugar Land.">
<script type="application/ld+json">{"@type": "AutoDealer", "name": "Alpha Motors", "telephone": "281-555-0101"}</script></head>
<body><h1>Alpha Motors</h1><p>Call 281-555-0101</p><a href="/inventory/2019-honda-civic">Civic</a><img src="/a.jpg" alt="Front of lot"></body></html>"""
VDP = """<html><head><title>2019 Honda Civic LX for sale in Sugar Land</title><meta name="description" content="2019 Honda Civic"></head>
<body><h1>2019 Honda Civic</h1><p>VIN 2HGFC2F59KH123456</p><p>$15,995</p></body></html>"""


class FakeSite:
    def __init__(self, home: str = HOME_V1, robots: str = "User-agent: *\nDisallow: /private/\n", status_429: bool = False):
        self.home, self.robots, self.status_429, self.requests = home, robots, status_429, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        path = request.url.path
        if self.status_429 and path not in ("/robots.txt", "/sitemap.xml"):
            return httpx.Response(429)
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if path == "/":
            return httpx.Response(200, html=self.home)
        if path == "/inventory/2019-honda-civic":
            return httpx.Response(200, html=VDP)
        if path.startswith("/private"):
            raise AssertionError("crawler fetched a robots-disallowed URL")
        return httpx.Response(404, text="nope")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url="https://alpha.example")


@pytest.fixture
def t(db):
    return two_clients(db)


def _site(db, t, authorized=True) -> SeoSite:
    site = SeoSite(client_id=t["alpha"].id, base_url="https://alpha.example", max_pages=10)
    db.add(site)
    db.flush()
    if authorized:
        seo_audit.authorize_site(db, site, db.query(User).filter_by(role="manager").first(), "Signed SEO authorization ref TEST-1")
    db.commit()
    return site


def _run(db, site, fake: FakeSite) -> SeoAudit:
    a = seo_audit.start_audit(db, site, None)
    seo_audit.run_audit(db, a, http_client=fake.client(), sleep=lambda s: None)
    db.commit()
    return a


def test_unauthorized_site_cannot_be_audited(db, t, client):
    site = _site(db, t, authorized=False)
    with pytest.raises(ValueError):
        seo_audit.start_audit(db, site, None)
    assert client.post(f"/api/v1/seo/sites/{site.id}/audits", headers=t["h"]["rep"]).status_code == 400


def test_audit_findings_are_evidenced_and_measured_vs_heuristic(db, t):
    site = _site(db, t)
    fake = FakeSite()
    a = _run(db, site, fake)
    assert a.status == "completed" and a.pages_crawled >= 2
    assert not any("/private" in u for u in fake.requests)  # robots.txt honoured
    codes = {i.code: i for i in db.query(SeoIssue).filter_by(site_id=site.id)}
    for code in ("meta_description_missing", "h1_multiple", "img_alt_missing", "broken_link", "sitemap_missing", "local_schema_missing",
                 "inventory_schema_missing", "title_short"):
        assert code in codes, code
    for i in codes.values():
        assert i.evidence and i.severity and i.recommended_fix and i.effort and i.responsible_party and i.status == "open"
    assert codes["broken_link"].url.endswith("/missing") and codes["broken_link"].detection == "measured"
    assert codes["spanish_content_opportunity"].detection == "heuristic"
    assert a.summary["measured"] > 0 and a.summary["heuristic"] >= 1


def test_reaudit_resolves_fixed_issues_with_before_after(db, t):
    site = _site(db, t)
    _run(db, site, FakeSite())
    title = db.query(SeoIssue).filter_by(site_id=site.id, code="title_short").one()
    assert title.before_value == "Alpha"
    a2 = _run(db, site, FakeSite(home=HOME_V2))
    db.refresh(title)
    assert title.status == "resolved" and title.resolved_audit_id == a2.id and "Not detected" in title.after_value
    still = db.query(SeoIssue).filter_by(site_id=site.id, code="sitemap_missing").one()
    assert still.status == "open" and still.last_seen_audit_id == a2.id


def test_crawler_stops_on_429_and_never_bypasses_robots(db, t):
    fake = FakeSite(status_429=True)
    res = Crawler("https://alpha.example", max_pages=10, delay_seconds=1, client=fake.client(), sleep=lambda s: None).crawl()
    assert res.stopped_reason and "429" in res.stopped_reason and len(res.pages) == 1
    blocked = FakeSite(robots="User-agent: *\nDisallow: /\n")
    res = Crawler("https://alpha.example", max_pages=10, delay_seconds=1, client=blocked.client(), sleep=lambda s: None).crawl()
    assert res.pages == [] and res.robots_blocked_urls


def test_audit_job_runs_through_worker_queue(db, t, monkeypatch):
    from dealerflow.jobs import run_pending

    site = _site(db, t)
    fake = FakeSite()
    orig = seo_audit.run_audit
    monkeypatch.setattr(seo_audit, "run_audit", lambda db_, a, **k: orig(db_, a, http_client=fake.client(), sleep=lambda s: None))
    a = seo_audit.start_audit(db, site, None)
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(SeoAudit, a.id).status == "completed"


def test_seo_never_writes_to_the_client_site(db, t):
    fake = FakeSite()
    methods = []
    orig = fake.handler
    fake.handler = lambda req: (methods.append(req.method), orig(req))[1]
    _run(db, _site(db, t), fake)
    assert set(methods) <= {"GET", "HEAD"}


def test_onboarding_blocks_premature_automation_and_activation(client, db, t):
    cid, hm, hr = t["alpha"].id, t["h"]["manager"], t["h"]["rep"]
    r = client.post(f"/api/v1/clients/{cid}/automation/inquiry_auto_response", headers=hm, json={"enabled": True})
    assert r.status_code == 400 and "Outstanding prerequisites" in r.json()["detail"]
    assert client.post(f"/api/v1/clients/{cid}/onboarding/service_activated", headers=hr, json={"status": "done"}).status_code == 400
    assert client.post(f"/api/v1/clients/{cid}/onboarding/authorizations", headers=hr, json={"status": "done"}).status_code == 400  # needs evidence
    for code, _ in onboarding.STEPS[1:11]:
        r = client.post(f"/api/v1/clients/{cid}/onboarding/{code}", headers=hr, json={"status": "done", "evidence": f"ref {code}"})
        assert r.status_code == 200, (code, r.text)
    assert client.post(f"/api/v1/clients/{cid}/onboarding/service_activated", headers=hr, json={"status": "done"}).status_code == 200
    # Steps done, but the messaging integration is not connected and verified yet.
    r = client.post(f"/api/v1/clients/{cid}/automation/inquiry_auto_response", headers=hm, json={"enabled": True})
    assert r.status_code == 400 and "customer_messaging" in r.json()["detail"]
    assert client.post(f"/api/v1/clients/{cid}/integrations", headers=hm, json={"kind": "customer_messaging", "provider": "test", "status": "connected"}).status_code == 400
    client.post(f"/api/v1/clients/{cid}/integrations", headers=hm, json={"kind": "customer_messaging", "provider": "test", "status": "connected",
                                                                       "note": "Verified with dealership test inbox"})
    assert client.post(f"/api/v1/clients/{cid}/automation/inquiry_auto_response", headers=hm, json={"enabled": True}).json()["enabled"] is True


def test_report_reflects_data_and_states_channels_and_attribution(client, db, t):
    from tests.dealer_helpers import SAMPLE_CSV, drain

    cid, h, hd = t["alpha"].id, t["h"]["rep"], t["h"]["alpha"]
    imp = client.post(f"/api/v1/dealers/{cid}/imports", headers=h, files={"file": ("inv.csv", SAMPLE_CSV.encode(), "text/csv")}).json()
    assert imp["counts"]["created"] == 3
    drain()  # Phase 2 worker drafts EN/ES listings
    ids = [li["id"] for li in client.get(f"/api/v1/dealers/{cid}/listings", headers=hd).json()["items"]]
    assert len(ids) == 3
    preview = client.post(f"/api/v1/dealers/{cid}/listings-batch/preview", headers=hd, json={"listing_ids": ids}).json()
    first = next(i["listing_id"] for i in preview["items"] if i["eligible"])
    done = client.post(f"/api/v1/dealers/{cid}/listings-batch/approve", headers=hd, json={
        "listing_ids": [first], "expected_count": 1, "confirm": True, "acknowledge_all_warnings": True}).json()
    assert len(done["approved"]) == 1
    pubs = client.get(f"/api/v1/dealers/{cid}/publications", headers=hd).json()
    assert [(p["channel"], p["status"]) for p in pubs] == [("facebook_marketplace", "manual_required")]
    sim = client.post(f"/api/v1/clients/{cid}/inquiries/simulated", headers=h, json={"body": "Is it available for a test drive?",
                                                                                   "provider_message_id": "m1"}).json()
    assert sim["simulated"] is True
    client.post(f"/api/v1/clients/{cid}/integrations", headers=t["h"]["manager"], json={"kind": "inventory_feed", "provider": "csv", "status": "connected",
                                                                                      "note": "Dealer emailed CSV export"})
    start, end = date.today().replace(day=1), date.today()
    r = client.post("/api/v1/client-reports", headers=h, json={"client_id": cid, "period_start": str(start), "period_end": str(end)}).json()
    d = r["data"]
    assert d["inventory"]["vehicles_processed"]["value"] == 3 and d["inventory"]["vehicles_added"]["value"] == 3
    assert d["listings"]["generated"]["value"] == 3 and d["listings"]["approved"]["value"] == 1
    assert d["listings"]["manual_publication_required"]["value"] == 1 and d["listings"]["publishing_confirmed_via_api"]["value"] == 0
    assert d["inquiries"]["received"]["value"] == 0 and d["inquiries"]["simulated_excluded"] == 1  # simulated never counted as real
    channels = {c["kind"]: c["status"] for c in d["channels"]}
    assert channels["inventory_feed"] == "connected" and channels["search_console"] == "not_connected"
    assert "makes no claim" in d["attribution_note"]
    assert d["seo"]["organic_search"]["value"] is None
    client.post(f"/api/v1/seo/clients/{cid}/search-metrics", headers=h, json={"csv": f"date,clicks,impressions,position\n{start},12,340,8.1\n"})
    d2 = client.post("/api/v1/client-reports", headers=h, json={"client_id": cid, "period_start": str(start), "period_end": str(end)}).json()["data"]
    assert d2["seo"]["organic_search"][0]["clicks"] == 12
    assert client.post("/api/v1/client-reports", headers=h, json={"client_id": cid, "period_start": str(end), "period_end": str(start - timedelta(days=1))}).status_code == 400


def test_report_email_is_simulated_without_authorization(client, db, t):
    from dealerflow.integrations.email import MockEmailProvider

    cid, h = t["alpha"].id, t["h"]["rep"]
    r = client.post("/api/v1/client-reports", headers=h, json={"client_id": cid, "period_start": "2026-09-01", "period_end": "2026-09-30"}).json()
    assert client.post(f"/api/v1/client-reports/{r['id']}/approve", headers=h).status_code == 403  # rep cannot approve
    assert client.post(f"/api/v1/client-reports/{r['id']}/approve", headers=t["h"]["manager"]).status_code == 200
    assert client.post(f"/api/v1/client-reports/{r['id']}/send", headers=h, json={"to": "owner@alpha.example"}).json()["delivery"] == "simulated"
    assert len(MockEmailProvider.outbox) == 1 and MockEmailProvider.outbox[0][1].simulated
    got = client.get(f"/api/v1/client-reports/{r['id']}", headers=h).json()
    assert got["status"] == "final" and got["delivery"] == "simulated"


def test_report_email_not_sent_with_real_provider_unless_authorized(db, t, monkeypatch):
    from dealerflow.config import get_settings
    from dealerflow.services import client_email

    monkeypatch.setattr(get_settings(), "email_provider", "smtp")
    monkeypatch.setattr(get_settings(), "client_email_sending_enabled", True)
    r = client_reports.generate_report(db, t["alpha"], date(2026, 9, 1), date(2026, 9, 30), None)
    client_reports.approve_report(db, r, db.query(User).filter_by(role="manager").first())
    sent = []
    monkeypatch.setattr(client_email, "get_email_provider", lambda: (_ for _ in ()).throw(AssertionError("must not send")))
    outcome = client_reports.send_report(db, r, "owner@alpha.example", None)
    assert outcome.startswith("not_sent") and "report_email" in outcome and not sent


def test_monthly_reports_job(db, t):
    from dealerflow.models import ClientReport
    from dealerflow.services.maintenance import monthly_reports_job

    t["alpha"].status = "active"
    db.commit()
    assert monthly_reports_job(db, {})["reports"] == 1
    assert monthly_reports_job(db, {})["reports"] == 0  # idempotent per period
    assert db.query(ClientReport).count() == 1


def test_lead_summary_reaches_the_correct_dealership(client, db, t):
    from dealerflow.integrations.email import MockEmailProvider

    cid, h = t["alpha"].id, t["h"]["rep"]
    conv = client.post(f"/api/v1/clients/{cid}/inquiries/simulated", headers=h, json={"body": "¿Está disponible para una prueba de manejo?",
                                                                                    "language": "es", "provider_message_id": "es-1"}).json()
    draft = client.post(f"/api/v1/clients/{cid}/conversations/{conv['conversation_id']}/draft", headers=h, json={"template": "test_drive"}).json()
    assert draft["language"] == "es" and "prueba de manejo" in draft["body"] and draft["status"] == "pending_approval"
    # Bravo's staff path cannot touch Alpha's conversation.
    assert client.post(f"/api/v1/clients/{t['bravo'].id}/conversations/{conv['conversation_id']}/messages/{draft['message_id']}/approve", headers=h).status_code == 404
    res = client.post(f"/api/v1/clients/{cid}/conversations/{conv['conversation_id']}/messages/{draft['message_id']}/approve", headers=h).json()
    assert res["summary_delivered"] is True and "Test-drive requested" in res["summary"]
    assert [m.to for m, _ in MockEmailProvider.outbox] == ["owner@alpha.example"]
