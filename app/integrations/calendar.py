"""Calendar + video-meeting providers.

* SandboxCalendar — deterministic, no network. Event ids/links are clearly fake
  (`sandbox-...`, `https://meet.sandbox.invalid/...`).
* GoogleCalendar  — Google Calendar API v3 (freeBusy query + events.insert with
  conferenceData.createRequest and conferenceDataVersion=1 for a Google Meet
  link). Uses an OAuth refresh token for the operator's calendar.
  STATUS: implemented against the documented API but NOT verified with real
  credentials in this repository's test runs.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings


@dataclass
class BookedEvent:
    provider: str
    event_id: str
    video_url: str | None
    starts_at: datetime
    ends_at: datetime


class CalendarError(Exception):
    pass


class SandboxCalendar:
    name = "sandbox"

    def __init__(self) -> None:
        self.busy: list[tuple[datetime, datetime]] = []

    def free_slots(self, after: datetime, count: int = 3, minutes: int = 30, tz: str = "America/Chicago") -> list[datetime]:
        return _business_slots(after, count, minutes, tz, self.busy)

    def book(self, start: datetime, minutes: int, title: str, attendee: str, description: str) -> BookedEvent:
        end = start + timedelta(minutes=minutes)
        for b0, b1 in self.busy:
            if start < b1 and end > b0:
                raise CalendarError("Slot no longer available")
        self.busy.append((start, end))
        eid = f"sandbox-{uuid.uuid4().hex[:12]}"
        return BookedEvent("sandbox", eid, f"https://meet.sandbox.invalid/{eid}", start, end)


def _business_slots(after: datetime, count: int, minutes: int, tz: str, busy: list[tuple[datetime, datetime]]) -> list[datetime]:
    """Candidate slots (UTC naive) at 10:00, 14:00 and 16:00 local on weekdays, starting tomorrow."""
    z = ZoneInfo(tz)
    local = after.replace(tzinfo=ZoneInfo("UTC")).astimezone(z)
    day = (local + timedelta(days=1)).date()
    out: list[datetime] = []
    while len(out) < count:
        if day.weekday() < 5:
            for hour in (10, 14, 16):
                start = datetime(day.year, day.month, day.day, hour, tzinfo=z).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
                end = start + timedelta(minutes=minutes)
                if not any(start < b1 and end > b0 for b0, b1 in busy):
                    out.append(start)
                if len(out) == count:
                    break
        day += timedelta(days=1)
    return out


class GoogleCalendar:
    name = "google"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    API = "https://www.googleapis.com/calendar/v3"

    def __init__(self, client: httpx.Client | None = None) -> None:
        s = get_settings()
        if not (s.google_oauth_client_id and s.google_oauth_client_secret and s.google_oauth_refresh_token):
            raise CalendarError("Google Calendar OAuth credentials are not configured")
        self.s = s
        self.http = client or httpx.Client(timeout=30)
        self._token: str | None = None

    def _auth(self) -> dict:
        if not self._token:
            r = self.http.post(self.TOKEN_URL, data={
                "client_id": self.s.google_oauth_client_id, "client_secret": self.s.google_oauth_client_secret,
                "refresh_token": self.s.google_oauth_refresh_token, "grant_type": "refresh_token",
            })
            if r.status_code != 200:
                raise CalendarError(f"OAuth token refresh failed ({r.status_code})")
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def _busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        r = self.http.post(f"{self.API}/freeBusy", headers=self._auth(), json={
            "timeMin": start.isoformat() + "Z", "timeMax": end.isoformat() + "Z", "items": [{"id": self.s.google_calendar_id}],
        })
        if r.status_code != 200:
            raise CalendarError(f"freeBusy failed ({r.status_code})")
        cal = r.json().get("calendars", {}).get(self.s.google_calendar_id, {})
        return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")).replace(tzinfo=None),
                 datetime.fromisoformat(b["end"].replace("Z", "+00:00")).replace(tzinfo=None)) for b in cal.get("busy", [])]

    def free_slots(self, after: datetime, count: int = 3, minutes: int = 30, tz: str = "America/Chicago") -> list[datetime]:
        return _business_slots(after, count, minutes, tz, self._busy(after, after + timedelta(days=14)))

    def book(self, start: datetime, minutes: int, title: str, attendee: str, description: str) -> BookedEvent:
        end = start + timedelta(minutes=minutes)
        if self._busy(start, end):
            raise CalendarError("Slot no longer available")
        r = self.http.post(
            f"{self.API}/calendars/{self.s.google_calendar_id}/events",
            params={"conferenceDataVersion": 1, "sendUpdates": "all"},
            headers=self._auth(),
            json={
                "summary": title, "description": description,
                "start": {"dateTime": start.isoformat() + "Z"}, "end": {"dateTime": end.isoformat() + "Z"},
                "attendees": [{"email": attendee}],
                "conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
            },
        )
        if r.status_code not in (200, 201):
            raise CalendarError(f"events.insert failed ({r.status_code})")
        ev = r.json()
        video = next((ep.get("uri") for ep in (ev.get("conferenceData") or {}).get("entryPoints", []) if ep.get("entryPointType") == "video"), None)
        return BookedEvent("google", ev["id"], video or ev.get("hangoutLink"), start, end)


_sandbox = SandboxCalendar()


def get_calendar(live: bool):
    if live and get_settings().calendar_provider == "google":
        return GoogleCalendar()
    return _sandbox
