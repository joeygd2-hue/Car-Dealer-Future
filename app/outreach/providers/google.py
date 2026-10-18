"""Live Google providers (Gmail API, Google Calendar API, Places API (New)).

Implemented against Google's documented REST endpoints (see
docs/research/outreach-providers-and-compliance-2026-10.md) but NOT verified
end to end: no OAuth client, consent/verification or API key was available
during development. They are only used when an operator configures the
integration, a platform admin authorises live use, and OUTREACH_LIVE_ALLOWED=true.

Credentials (stored encrypted): Gmail/Calendar use an OAuth refresh token
({"client_id", "client_secret", "refresh_token"}); Places uses an API key.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime

import httpx

from app.core.time import sha256
from app.outreach.providers.base import (
    CalendarProvider, Candidate, DiscoveryProvider, EmailProvider, InboundEmail, OutboundEmail, ProviderError, SendResult, Slot,
)

_transport: httpx.BaseTransport | None = None


def set_transport(t):
    global _transport
    _transport = t


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, transport=_transport)


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 429 or resp.status_code >= 500:
        raise ProviderError(f"{what}: HTTP {resp.status_code}", retryable=True)
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except ValueError:
            msg = resp.text[:200]
        raise ProviderError(f"{what}: HTTP {resp.status_code} {msg}")


class _OAuth:
    def __init__(self, creds: dict):
        self.creds = creds
        self._token, self._exp = None, 0.0

    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        try:
            with _client() as c:
                r = c.post("https://oauth2.googleapis.com/token", data={
                    "client_id": self.creds["client_id"], "client_secret": self.creds["client_secret"],
                    "refresh_token": self.creds["refresh_token"], "grant_type": "refresh_token"})
        except httpx.HTTPError as exc:
            raise ProviderError(f"OAuth token refresh failed: {type(exc).__name__}", retryable=True) from exc
        _raise_for(r, "OAuth token refresh")
        body = r.json()
        self._token, self._exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token


class GmailProvider(EmailProvider):
    """Scopes: gmail.send (Sensitive) and gmail.readonly (Restricted -> verification/security assessment
    unless the OAuth app is Internal to the agency's Workspace)."""
    live = True
    name = "gmail"
    BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, creds: dict, config: dict):
        self.oauth = _OAuth(creds)
        self.config = config

    def _h(self):
        return {"Authorization": f"Bearer {self.oauth.token()}"}

    def send(self, email: OutboundEmail) -> SendResult:
        msg = EmailMessage()
        msg["To"] = email.to
        msg["From"] = f"{email.from_name} <{email.from_address}>"
        msg["Subject"] = email.subject
        msg["Message-ID"] = make_msgid(idstring=sha256(email.idempotency_key)[:16])
        if email.in_reply_to:
            msg["In-Reply-To"] = email.in_reply_to
            msg["References"] = email.in_reply_to
        if email.unsubscribe_url:
            msg["List-Unsubscribe"] = f"<{email.unsubscribe_url}>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content(email.body)
        body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if email.thread_id:
            body["threadId"] = email.thread_id
        try:
            with _client() as c:
                r = c.post(f"{self.BASE}/messages/send", headers=self._h(), json=body)
        except httpx.HTTPError as exc:
            # Outcome unknown: the caller must NOT blindly resend (see agent duplicate guard).
            raise ProviderError(f"Gmail send outcome unknown: {type(exc).__name__}", retryable=False) from exc
        _raise_for(r, "Gmail send")
        data = r.json()
        return SendResult(data["id"], data.get("threadId"))

    def fetch_inbound(self, since: datetime | None, limit: int = 50) -> list[InboundEmail]:
        q = "in:inbox -from:me"
        if since:
            q += f" after:{int(since.timestamp())}"
        try:
            with _client() as c:
                r = c.get(f"{self.BASE}/messages", headers=self._h(), params={"q": q, "maxResults": limit})
                _raise_for(r, "Gmail list")
                out = []
                for ref in r.json().get("messages", []):
                    m = c.get(f"{self.BASE}/messages/{ref['id']}", headers=self._h(), params={"format": "full"})
                    _raise_for(m, "Gmail get")
                    out.append(_parse_gmail(m.json()))
                return out
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gmail fetch failed: {type(exc).__name__}", retryable=True) from exc

    def health_check(self):
        try:
            with _client() as c:
                r = c.get(f"{self.BASE}/profile", headers=self._h())
            _raise_for(r, "Gmail profile")
            return "ok", "profile reachable"
        except ProviderError as exc:
            return "down", str(exc)


def _parse_gmail(m: dict) -> InboundEmail:
    headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}

    def walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "replace")
        for p in part.get("parts", []) or []:
            t = walk(p)
            if t:
                return t
        return ""
    body = walk(m.get("payload", {})) or m.get("snippet", "")
    try:
        received = parsedate_to_datetime(headers.get("date")) if headers.get("date") else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        received = datetime.now(timezone.utc)
    frm = headers.get("from", "")
    addr = frm.split("<")[-1].rstrip(">").strip() if "<" in frm else frm.strip()
    return InboundEmail(provider_message_id=m["id"], thread_id=m.get("threadId"), from_address=addr.lower(),
                        to_address=headers.get("to", ""), subject=headers.get("subject", ""), body=body,
                        received_at=received, in_reply_to=headers.get("in-reply-to"), headers=headers)


class GoogleCalendarProvider(CalendarProvider):
    """Scopes: calendar.events (+ calendar.freebusy). Creates Google Meet links via conferenceData."""
    live = True
    name = "google_calendar"
    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, creds: dict, config: dict):
        self.oauth = _OAuth(creds)
        self.calendar_id = config.get("calendar_id", "primary")

    def busy(self, start: datetime, end: datetime) -> list[Slot]:
        try:
            with _client() as c:
                r = c.post(f"{self.BASE}/freeBusy", headers={"Authorization": f"Bearer {self.oauth.token()}"},
                           json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": self.calendar_id}]})
        except httpx.HTTPError as exc:
            raise ProviderError(f"Calendar freeBusy failed: {type(exc).__name__}", retryable=True) from exc
        _raise_for(r, "Calendar freeBusy")
        cal = r.json().get("calendars", {}).get(self.calendar_id, {})
        return [Slot(datetime.fromisoformat(b["start"].replace("Z", "+00:00")), datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                for b in cal.get("busy", [])]

    def create_event(self, *, start, end, attendee_email, summary, description, idempotency_key):
        # Client-supplied event id (base32hex: 0-9a-v) makes creation idempotent: a retry gets 409.
        event_id = "cdf" + sha256(idempotency_key)[:40]
        body = {"id": event_id, "summary": summary, "description": description,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
                "attendees": [{"email": attendee_email}],
                "conferenceData": {"createRequest": {"requestId": event_id, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}}
        try:
            with _client() as c:
                h = {"Authorization": f"Bearer {self.oauth.token()}"}
                r = c.post(f"{self.BASE}/calendars/{self.calendar_id}/events", headers=h, json=body,
                           params={"conferenceDataVersion": 1, "sendUpdates": "all"})
                if r.status_code == 409:
                    r = c.get(f"{self.BASE}/calendars/{self.calendar_id}/events/{event_id}", headers=h)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Calendar insert failed: {type(exc).__name__}", retryable=True) from exc
        _raise_for(r, "Calendar insert")
        data = r.json()
        link = data.get("hangoutLink") or next((e.get("uri") for e in data.get("conferenceData", {}).get("entryPoints", [])
                                                if e.get("entryPointType") == "video"), None)
        return data["id"], link


class GooglePlacesProvider(DiscoveryProvider):
    """Places API (New) Text Search. Requesting websiteUri/nationalPhoneNumber bills at the
    Enterprise SKU (~$35 per 1,000 calls; 1,000 free/month as of the research date).
    ToS: only place_id may be stored indefinitely; other content is kept in
    Prospect.source_detail with a timestamp and purged after 30 days."""
    live = True
    name = "google_places"
    estimated_cost_per_call = 0.035
    FIELDS = ("places.id,places.displayName,places.formattedAddress,places.addressComponents,places.types,"
              "places.websiteUri,places.nationalPhoneNumber,places.businessStatus")

    def __init__(self, api_key: str, config: dict):
        self.key = api_key
        self.config = config

    def search(self, query: str, limit: int = 20) -> list[Candidate]:
        try:
            with _client() as c:
                r = c.post("https://places.googleapis.com/v1/places:searchText",
                           headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": self.FIELDS},
                           json={"textQuery": query, "includedType": "car_dealer", "pageSize": min(limit, 20)})
        except httpx.HTTPError as exc:
            raise ProviderError(f"Places search failed: {type(exc).__name__}", retryable=True) from exc
        _raise_for(r, "Places search")
        out = []
        for p in r.json().get("places", []):
            comps = {t: c.get("shortText") for c in p.get("addressComponents", []) for t in c.get("types", [])}
            out.append(Candidate(business_name=(p.get("displayName") or {}).get("text", ""), source="google_places",
                                 source_ref=p.get("id"), website=p.get("websiteUri"), phone=p.get("nationalPhoneNumber"),
                                 address=p.get("formattedAddress"), city=comps.get("locality"),
                                 state=comps.get("administrative_area_level_1"), postal_code=comps.get("postal_code"),
                                 types=p.get("types", []), business_status=p.get("businessStatus"),
                                 detail={"places_fetched_at": datetime.now(timezone.utc).isoformat()}))
        return out


def creds_from_secret(secret: str | None) -> dict:
    if not secret:
        raise ProviderError("Integration has no credentials configured")
    try:
        return json.loads(secret)
    except json.JSONDecodeError:
        return {"api_key": secret}
