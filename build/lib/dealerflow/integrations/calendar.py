"""Calendar providers: real free/busy lookups and event creation.

- ``MockCalendarProvider`` (SIMULATED): in-memory busy blocks and events.
- ``GoogleCalendarProvider``: freeBusy query + events insert/patch/delete.
  Scopes: calendar.freebusy (availability) and calendar.events (create the meeting event).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import get_settings
from .errors import IntegrationError, NotConfiguredError
from .http import request


@dataclass
class BusyBlock:
    start: datetime  # naive UTC
    end: datetime


@dataclass
class CalendarEvent:
    event_id: str
    html_link: str | None
    raw: dict
    simulated: bool


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


class CalendarProvider:
    name = "base"
    simulated = False

    def busy(self, start: datetime, end: datetime) -> list[BusyBlock]:
        raise NotImplementedError

    def create_event(self, *, start: datetime, end: datetime, summary: str, description: str,
                     attendee_email: str | None, location: str | None, request_id: str) -> CalendarEvent:
        raise NotImplementedError

    def update_event(self, event_id: str, *, start: datetime, end: datetime) -> CalendarEvent:
        raise NotImplementedError

    def delete_event(self, event_id: str) -> None:
        raise NotImplementedError

    def check(self) -> str:
        raise NotImplementedError


class MockCalendarProvider(CalendarProvider):
    name = "mock"
    simulated = True
    busy_blocks: list[BusyBlock] = []
    events: dict[str, dict] = {}
    fail_next: list[Exception] = []

    @classmethod
    def reset(cls) -> None:
        cls.busy_blocks, cls.events, cls.fail_next = [], {}, []

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    def busy(self, start: datetime, end: datetime) -> list[BusyBlock]:
        self._maybe_fail()
        blocks = [b for b in type(self).busy_blocks if b.end > start and b.start < end]
        blocks += [BusyBlock(e["start"], e["end"]) for e in type(self).events.values()
                   if e["status"] != "cancelled" and e["end"] > start and e["start"] < end]
        return blocks

    def create_event(self, *, start, end, summary, description, attendee_email, location, request_id) -> CalendarEvent:
        self._maybe_fail()
        for eid, e in type(self).events.items():  # idempotent on request_id
            if e["request_id"] == request_id:
                return CalendarEvent(eid, None, {"id": eid, "simulated": True}, True)
        eid = f"simevt-{uuid.uuid4().hex[:10]}"
        type(self).events[eid] = {"start": start, "end": end, "summary": summary, "attendee": attendee_email,
                                  "location": location, "status": "confirmed", "request_id": request_id}
        return CalendarEvent(eid, None, {"id": eid, "status": "confirmed", "simulated": True}, True)

    def update_event(self, event_id, *, start, end) -> CalendarEvent:
        self._maybe_fail()
        e = type(self).events.get(event_id)
        if not e:
            raise IntegrationError("SIMULATED calendar: event not found")
        e["start"], e["end"] = start, end
        return CalendarEvent(event_id, None, {"id": event_id, "simulated": True}, True)

    def delete_event(self, event_id) -> None:
        self._maybe_fail()
        if event_id in type(self).events:
            type(self).events[event_id]["status"] = "cancelled"

    def check(self) -> str:
        return "SIMULATED calendar (availability is not real)"


class GoogleCalendarProvider(CalendarProvider):
    name = "google"
    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self) -> None:
        from .google_auth import access_token

        self._token = access_token
        self.cal = get_settings().google_calendar_id

    def _h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def busy(self, start: datetime, end: datetime) -> list[BusyBlock]:
        r = request("POST", f"{self.BASE}/freeBusy", provider="google_calendar", headers=self._h(),
                    json={"timeMin": _iso(start), "timeMax": _iso(end), "items": [{"id": self.cal}]})
        cal = r.json().get("calendars", {}).get(self.cal, {})
        if cal.get("errors"):
            raise IntegrationError(f"Google freeBusy error: {cal['errors']}")
        return [BusyBlock(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy", [])]

    def create_event(self, *, start, end, summary, description, attendee_email, location, request_id) -> CalendarEvent:
        body = {"summary": summary, "description": description, "location": location,
                "start": {"dateTime": _iso(start)}, "end": {"dateTime": _iso(end)},
                # Deterministic id makes a retried insert return 409 instead of a duplicate event.
                "id": "df" + uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex}
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]
        try:
            r = request("POST", f"{self.BASE}/calendars/{self.cal}/events", provider="google_calendar",
                        headers=self._h(), json=body, params={"sendUpdates": "all"}, retries=1)
            d = r.json()
        except IntegrationError as exc:
            if "HTTP 409" not in str(exc):
                raise
            d = request("GET", f"{self.BASE}/calendars/{self.cal}/events/{body['id']}", provider="google_calendar",
                        headers=self._h()).json()
        return CalendarEvent(d["id"], d.get("htmlLink"), d, False)

    def update_event(self, event_id, *, start, end) -> CalendarEvent:
        r = request("PATCH", f"{self.BASE}/calendars/{self.cal}/events/{event_id}", provider="google_calendar",
                    headers=self._h(), params={"sendUpdates": "all"},
                    json={"start": {"dateTime": _iso(start)}, "end": {"dateTime": _iso(end)}})
        d = r.json()
        return CalendarEvent(d["id"], d.get("htmlLink"), d, False)

    def delete_event(self, event_id) -> None:
        request("DELETE", f"{self.BASE}/calendars/{self.cal}/events/{event_id}", provider="google_calendar",
                headers=self._h(), params={"sendUpdates": "all"})

    def check(self) -> str:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        from datetime import timedelta
        self.busy(now, now + timedelta(hours=1))
        return f"Google Calendar freeBusy ok for {self.cal}"


def get_calendar_provider() -> CalendarProvider:
    kind = get_settings().calendar_provider
    if kind == "mock":
        return MockCalendarProvider()
    if kind == "google":
        return GoogleCalendarProvider()
    raise NotConfiguredError(f"Unknown calendar provider {kind!r}")
