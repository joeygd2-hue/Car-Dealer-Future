"""Real provider clients exercised against mocked HTTP (httpx.MockTransport).

These check request shape and response parsing against the documented APIs. They are NOT a
substitute for a live test with real credentials, which has not been done (see known-limitations.md).
"""

import base64
import json

import httpx
import pytest

from dealerflow.config import get_settings
from dealerflow.integrations import http as http_mod

REAL_CLIENT = httpx.Client


@pytest.fixture
def routes(monkeypatch):
    table = {}
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        key = f"{request.method} {request.url.copy_with(query=None)}"
        resp = table.get(key)
        if resp is None:
            return httpx.Response(404, text=f"unmocked {key}")
        return resp(request) if callable(resp) else resp

    monkeypatch.setattr(http_mod.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler)))
    table["_seen"] = seen
    return table


def _env(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def _google(monkeypatch, routes):
    from dealerflow.integrations import google_auth

    google_auth.clear_cache()
    _env(monkeypatch, DF_GOOGLE_CLIENT_ID="cid", DF_GOOGLE_CLIENT_SECRET="cs", DF_GOOGLE_REFRESH_TOKEN="rt")
    routes["POST https://oauth2.googleapis.com/token"] = httpx.Response(200, json={"access_token": "at", "expires_in": 3600})


def test_gmail_send_and_fetch(monkeypatch, routes):
    from dealerflow.integrations.email import GmailApiProvider, OutgoingEmail

    _google(monkeypatch, routes)
    base = "https://gmail.googleapis.com/gmail/v1/users/me"
    routes[f"POST {base}/messages/send"] = httpx.Response(200, json={"id": "g1", "threadId": "t1"})
    raw = (b"From: Owner <owner@dealer.example>\r\nTo: me@x.example\r\nSubject: Re: hi\r\n"
           b"Message-ID: <r1@dealer.example>\r\nIn-Reply-To: <orig@x.example>\r\nDate: Mon, 19 Oct 2026 10:00:00 -0500\r\n\r\n"
           b"Sounds good\r\n\r\nOn Mon wrote:\r\n> quoted")
    routes[f"GET {base}/messages"] = httpx.Response(200, json={"messages": [{"id": "g2"}]})
    routes[f"GET {base}/messages/g2"] = httpx.Response(200, json={
        "id": "g2", "threadId": "t1", "internalDate": "1792422000000", "raw": base64.urlsafe_b64encode(raw).decode()})
    p = GmailApiProvider()
    res = p.send(OutgoingEmail(to="owner@dealer.example", subject="Hello", body="Hi", from_name="Me",
                               from_email="me@x.example", unsubscribe_url="https://x.example/u/t", thread_id=None))
    assert res.provider_message_id == "g1" and res.thread_id == "t1" and res.rfc_message_id.startswith("<")
    sent = [r for r in routes["_seen"] if r.url.path.endswith("/send")][0]
    mime = base64.urlsafe_b64decode(json.loads(sent.content)["raw"]).decode()
    assert "List-Unsubscribe: <https://x.example/u/t>" in mime and "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in mime
    assert sent.headers["authorization"] == "Bearer at"
    items, cursor = p.fetch_inbound(None)
    assert items[0].from_email == "owner@dealer.example" and items[0].in_reply_to == "<orig@x.example>"
    assert items[0].thread_id == "t1" and cursor == "1792422000"


def test_google_calendar_freebusy_and_idempotent_insert(monkeypatch, routes):
    from datetime import datetime

    from dealerflow.integrations.calendar import GoogleCalendarProvider

    _google(monkeypatch, routes)
    base = "https://www.googleapis.com/calendar/v3"
    routes[f"POST {base}/freeBusy"] = httpx.Response(200, json={"calendars": {"primary": {"busy": [
        {"start": "2026-10-20T15:00:00Z", "end": "2026-10-20T16:00:00Z"}]}}})
    routes[f"POST {base}/calendars/primary/events"] = httpx.Response(409, json={"error": "duplicate"})

    def get_event(req):
        return httpx.Response(200, json={"id": req.url.path.rsplit("/", 1)[1], "htmlLink": "https://cal/x"})

    cal = GoogleCalendarProvider()
    busy = cal.busy(datetime(2026, 10, 20), datetime(2026, 10, 21))
    assert busy[0].start == datetime(2026, 10, 20, 15)
    # 409 on insert (retry after a lost response) -> fetch the existing event instead of duplicating
    eid = "df" + __import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, "req-1").hex
    routes[f"GET {base}/calendars/primary/events/{eid}"] = get_event
    ev = cal.create_event(start=datetime(2026, 10, 22, 15), end=datetime(2026, 10, 22, 15, 30), summary="s",
                          description="d", attendee_email="a@b.example", location="zoom", request_id="req-1")
    assert ev.event_id == eid


def test_zoom_server_to_server(monkeypatch, routes):
    from datetime import datetime

    from dealerflow.integrations.zoom import ServerToServerZoomProvider

    _env(monkeypatch, DF_ZOOM_ACCOUNT_ID="acc", DF_ZOOM_CLIENT_ID="zid", DF_ZOOM_CLIENT_SECRET="zs")
    ServerToServerZoomProvider._token = None
    routes["POST https://zoom.us/oauth/token"] = httpx.Response(200, json={"access_token": "zt", "expires_in": 3600})
    routes["POST https://api.zoom.us/v2/users/me/meetings"] = httpx.Response(201, json={
        "id": 85012345678, "join_url": "https://zoom.us/j/85012345678", "start_url": "https://zoom.us/s/x"})
    m = ServerToServerZoomProvider().create_meeting(topic="Intro", start=datetime(2026, 10, 22, 15), duration_minutes=30,
                                                    timezone_name="America/Chicago", agenda="a")
    assert m.meeting_id == "85012345678" and not m.simulated and "start_url" not in m.raw
    tok = [r for r in routes["_seen"] if r.url.host == "zoom.us"][0]
    assert tok.url.params["grant_type"] == "account_credentials" and tok.url.params["account_id"] == "acc"
    assert tok.headers["authorization"] == "Basic " + base64.b64encode(b"zid:zs").decode()
    body = json.loads([r for r in routes["_seen"] if r.url.host == "api.zoom.us"][0].content)
    assert body["type"] == 2 and body["start_time"] == "2026-10-22T15:00:00Z" and body["settings"]["auto_recording"] == "none"


def test_google_places_parsing(monkeypatch, routes):
    from dealerflow.integrations.discovery import google_places_search

    _env(monkeypatch, DF_GOOGLE_PLACES_API_KEY="pk")
    routes["POST https://places.googleapis.com/v1/places:searchText"] = httpx.Response(200, json={"places": [
        {"id": "ChIJx", "displayName": {"text": "Real-ish Motors"}, "businessStatus": "OPERATIONAL",
         "addressComponents": [{"longText": "123", "types": ["street_number"]}, {"longText": "Main St", "types": ["route"]},
                               {"longText": "Sugar Land", "types": ["locality"]},
                               {"longText": "Texas", "shortText": "TX", "types": ["administrative_area_level_1"]},
                               {"longText": "77478", "types": ["postal_code"]}],
         "location": {"latitude": 29.6, "longitude": -95.6}, "nationalPhoneNumber": "(281) 555-0100",
         "websiteUri": "https://realish.example", "googleMapsUri": "https://maps.google.com/?cid=1"},
        {"id": "closed", "displayName": {"text": "Closed Cars"}, "businessStatus": "CLOSED_PERMANENTLY"}]})
    recs = google_places_search("used car dealer")
    assert len(recs) == 1
    r = recs[0]
    assert (r.external_id, r.address_line, r.city, r.state, r.postal_code) == ("ChIJx", "123 Main St", "Sugar Land", "TX", "77478")
    assert r.dealership_type is None  # never guessed
    req = routes["_seen"][0]
    assert req.headers["x-goog-api-key"] == "pk" and "places.id" in req.headers["x-goog-fieldmask"]


def test_overpass_parsing(monkeypatch, routes):
    from dealerflow.integrations.discovery import osm_search

    routes[f"POST {get_settings().overpass_url}"] = httpx.Response(200, json={"elements": [
        {"type": "node", "id": 1, "lat": 29.6, "lon": -95.6, "tags": {"shop": "car", "name": "OSM Used Cars", "second_hand": "only",
                                                                   "phone": "+1 281 555 0123", "addr:city": "Stafford"}},
        {"type": "way", "id": 2, "center": {"lat": 29.7, "lon": -95.5}, "tags": {"shop": "car", "name": "Brand Toyota", "brand": "Toyota"}},
        {"type": "node", "id": 3, "lat": 1, "lon": 1, "tags": {"shop": "car"}}]})
    recs = osm_search()
    assert [r.dealership_type for r in recs] == ["independent_used", "franchise"]
    assert recs[0].source_url == "https://www.openstreetmap.org/node/1" and "ODbL" in recs[0].notes


def test_website_research_respects_robots(monkeypatch, routes):
    from dealerflow.integrations import website

    _env(monkeypatch, DF_WEBSITE_RESEARCH_ENABLED="true")
    monkeypatch.setattr(website.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="User-agent: *\nDisallow: /") if req.url.path == "/robots.txt"
        else httpx.Response(200, text="<html>should not be fetched</html>"))))
    obs = website.research_site("https://blocked.example/")
    assert obs.blocked_by_robots and obs.pages_fetched == []


def test_website_research_signals(monkeypatch, routes):
    from dealerflow.integrations import website

    _env(monkeypatch, DF_WEBSITE_RESEARCH_ENABLED="true")
    home = ('<html lang="en"><head><title>Dealer</title><meta name="viewport" content="x"></head><body>'
            '<a href="/used-cars">Inventory</a><a href="mailto:sales@dealer.example">email</a>'
            '<p>Se habla español</p><form></form></body></html>')
    inv = "<div>2019 Toyota Camry</div><div>2020 Honda Civic</div><div>2018 Ford F-150</div>"

    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=inv if req.url.path == "/used-cars" else home)

    client = REAL_CLIENT(transport=httpx.MockTransport(h), follow_redirects=True)
    obs = website.research_site("https://dealer.example/", client=client)
    sig = {f: v for f, v, _ in obs.signals}
    assert sig["signal:spanish_content"] == "true" and sig["signal:contact_form"] == "true"
    assert sig["signal:inventory_count"] == "3" and obs.inventory_url == "https://dealer.example/used-cars"
    assert obs.public_emails == ["sales@dealer.example"]


def test_disabled_website_research_refuses(monkeypatch):
    from dealerflow.integrations.errors import NotConfiguredError
    from dealerflow.integrations.website import research_site

    with pytest.raises(NotConfiguredError):
        research_site("https://x.example")


def test_smtp_send_and_auth_failure(monkeypatch):
    import smtplib

    from dealerflow.integrations import email as email_mod
    from dealerflow.integrations.errors import CredentialError

    _env(monkeypatch, DF_SMTP_HOST="smtp.x.example", DF_SMTP_USERNAME="u", DF_SMTP_PASSWORD="p")
    sent = []

    class FakeSMTP:
        fail_login = False

        def __init__(self, host, port, timeout):
            assert (host, port) == ("smtp.x.example", 587)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            self.tls = True

        def login(self, u, p):
            if FakeSMTP.fail_login:
                raise smtplib.SMTPAuthenticationError(535, b"bad")

        def send_message(self, msg):
            sent.append(msg)
            return {}

    monkeypatch.setattr(email_mod.smtplib, "SMTP", FakeSMTP)
    p = email_mod.SmtpImapProvider()
    res = p.send(email_mod.OutgoingEmail(to="a@b.example", subject="s", body="b", from_name="Me", from_email="me@x.example",
                                         unsubscribe_url="https://x.example/u/t", unsubscribe_mailto="me@x.example"))
    assert res.rfc_message_id == sent[0]["Message-ID"]
    assert "mailto:me@x.example" in sent[0]["List-Unsubscribe"]
    FakeSMTP.fail_login = True
    with pytest.raises(CredentialError):
        p.send(email_mod.OutgoingEmail(to="a@b.example", subject="s", body="b", from_name="", from_email="me@x.example"))


def test_twilio_bridges_agent_first_without_recording(monkeypatch, routes):
    from dealerflow.integrations.calls import TwilioCallProvider

    _env(monkeypatch, DF_TWILIO_ACCOUNT_SID="AC1", DF_TWILIO_AUTH_TOKEN="tok", DF_TWILIO_CALLER_ID="+12815550100",
         DF_AGENT_PHONE="+12815550199")
    routes["POST https://api.twilio.com/2010-04-01/Accounts/AC1/Calls.json"] = httpx.Response(201, json={"sid": "CA1"})
    res = TwilioCallProvider().start_call("+12815550123", call_id=7)
    assert res.provider_call_id == "CA1"
    form = dict(x.split("=", 1) for x in routes["_seen"][0].content.decode().split("&"))
    from urllib.parse import unquote_plus
    assert unquote_plus(form["To"]) == "+12815550199"  # the operator's phone rings first
    twiml = unquote_plus(form["Twiml"])
    assert 'record="do-not-record"' in twiml and "+12815550123" in twiml and 'callerId="+12815550100"' in twiml
