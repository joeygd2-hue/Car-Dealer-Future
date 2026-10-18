"""Google Calendar API v3 (freeBusy + events) using an OAuth refresh token."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.integrations.base import BusyInterval, IntegrationError

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class GoogleCalendar:
    name = "google_calendar"
    simulated = False

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, calendar_id: str,
                 client: httpx.Client | None = None):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.calendar_id = calendar_id
        self.client = client or httpx.Client(timeout=15)
        self._token: str | None = None

    def _auth(self) -> dict:
        if not self._token:
            resp = self.client.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
            if resp.status_code != 200:
                raise IntegrationError(f"Google token refresh failed: {resp.status_code}", retryable=resp.status_code >= 500)
            self._token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def _check(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            raise IntegrationError(f"Google Calendar {resp.status_code}: {resp.text[:200]}",
                                   retryable=resp.status_code >= 500 or resp.status_code == 429)
        return resp.json() if resp.content else {}

    def free_busy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        data = self._check(self.client.post(f"{API}/freeBusy", headers=self._auth(), json={
            "timeMin": _iso(start), "timeMax": _iso(end), "items": [{"id": self.calendar_id}]}))
        busy = data.get("calendars", {}).get(self.calendar_id, {}).get("busy", [])
        return [BusyInterval(_parse(b["start"]), _parse(b["end"])) for b in busy]

    def create_event(self, *, summary, start, end, description, attendees, location=None) -> str:
        body = {"summary": summary, "description": description, "start": {"dateTime": _iso(start)},
                "end": {"dateTime": _iso(end)}, "attendees": [{"email": a} for a in attendees]}
        if location:
            body["location"] = location
        data = self._check(self.client.post(f"{API}/calendars/{self.calendar_id}/events", headers=self._auth(),
                                            params={"sendUpdates": "all"}, json=body))
        return data["id"]

    def cancel_event(self, event_id: str) -> None:
        self._check(self.client.delete(f"{API}/calendars/{self.calendar_id}/events/{event_id}", headers=self._auth()))


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
