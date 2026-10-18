"""Zoom meetings via a Server-to-Server OAuth app.

Token: POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=...
Create: POST https://api.zoom.us/v2/users/{userId}/meetings
Webhooks are verified with the app's secret token (HMAC-SHA256 of "v0:{timestamp}:{body}").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import get_settings
from .errors import IntegrationError, NotConfiguredError
from .http import request


@dataclass
class ZoomMeeting:
    meeting_id: str
    join_url: str
    start_url: str | None
    raw: dict
    simulated: bool


class ZoomProvider:
    name = "base"
    simulated = False

    def create_meeting(self, *, topic: str, start: datetime, duration_minutes: int, timezone_name: str,
                       agenda: str) -> ZoomMeeting:
        raise NotImplementedError

    def update_meeting(self, meeting_id: str, *, start: datetime, duration_minutes: int) -> None:
        raise NotImplementedError

    def find_meeting(self, *, topic: str, start: datetime) -> ZoomMeeting | None:
        """Find an upcoming meeting with this topic and start (used after an ambiguous create)."""
        raise NotImplementedError

    def delete_meeting(self, meeting_id: str) -> None:
        raise NotImplementedError

    def check(self) -> str:
        raise NotImplementedError


class MockZoomProvider(ZoomProvider):
    name = "mock_zoom"
    simulated = True
    meetings: dict[str, dict] = {}
    fail_next: list[Exception] = []

    @classmethod
    def reset(cls) -> None:
        cls.meetings, cls.fail_next = {}, []

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    def create_meeting(self, *, topic, start, duration_minutes, timezone_name, agenda) -> ZoomMeeting:
        self._maybe_fail()
        mid = str(80000000000 + len(type(self).meetings))
        url = f"https://simulated.invalid/j/{mid}"
        type(self).meetings[mid] = {"topic": topic, "start": start, "duration": duration_minutes, "status": "waiting"}
        return ZoomMeeting(mid, url, None, {"id": mid, "join_url": url, "simulated": True}, True)

    def update_meeting(self, meeting_id, *, start, duration_minutes) -> None:
        self._maybe_fail()
        type(self).meetings[meeting_id].update(start=start, duration=duration_minutes)

    def find_meeting(self, *, topic, start) -> ZoomMeeting | None:
        self._maybe_fail()
        for mid, m in type(self).meetings.items():
            if m["topic"] == topic and m["start"] == start:
                url = f"https://simulated.invalid/j/{mid}"
                return ZoomMeeting(mid, url, None, {"id": mid, "join_url": url, "simulated": True, "recovered": True}, True)
        return None

    def delete_meeting(self, meeting_id) -> None:
        self._maybe_fail()
        type(self).meetings.pop(meeting_id, None)

    def check(self) -> str:
        return "SIMULATED Zoom (no real meeting is created)"


class ServerToServerZoomProvider(ZoomProvider):
    name = "zoom"
    API = "https://api.zoom.us/v2"
    _lock = threading.Lock()
    _token: tuple[str, float] | None = None

    def __init__(self) -> None:
        s = get_settings()
        if not (s.zoom_account_id and s.zoom_client_id and s.zoom_client_secret.get_secret_value()):
            raise NotConfiguredError("Zoom is not configured (DF_ZOOM_ACCOUNT_ID / _CLIENT_ID / _CLIENT_SECRET)")
        self.s = s

    def _access_token(self) -> str:
        with self._lock:
            tok = type(self)._token
            if tok and tok[1] > time.time() + 60:
                return tok[0]
            basic = base64.b64encode(
                f"{self.s.zoom_client_id}:{self.s.zoom_client_secret.get_secret_value()}".encode()).decode()
            r = request("POST", "https://zoom.us/oauth/token", provider="zoom_oauth", retries=2,
                        headers={"Authorization": f"Basic {basic}"},
                        params={"grant_type": "account_credentials", "account_id": self.s.zoom_account_id})
            d = r.json()
            type(self)._token = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
            return d["access_token"]

    def _h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def create_meeting(self, *, topic, start, duration_minutes, timezone_name, agenda) -> ZoomMeeting:
        body = {"topic": topic[:200], "type": 2, "start_time": start.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration": duration_minutes, "timezone": timezone_name, "agenda": agenda[:2000],
                "settings": {"join_before_host": False, "waiting_room": True, "auto_recording": "none"}}
        r = request("POST", f"{self.API}/users/{self.s.zoom_user_id}/meetings", provider="zoom", headers=self._h(),
                    json=body, retries=0, rate=(10, 1.0))
        d = r.json()
        if not d.get("id") or not d.get("join_url"):
            raise IntegrationError(f"Zoom response missing id/join_url: {list(d)}")
        return ZoomMeeting(str(d["id"]), d["join_url"], d.get("start_url"), {k: d.get(k) for k in ("id", "uuid", "join_url", "start_time", "duration", "timezone")}, False)

    def find_meeting(self, *, topic, start) -> ZoomMeeting | None:
        want = start.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = request("GET", f"{self.API}/users/{self.s.zoom_user_id}/meetings", provider="zoom", headers=self._h(),
                    params={"type": "upcoming", "page_size": 300})
        for d in r.json().get("meetings", []):
            if d.get("topic") == topic[:200] and d.get("start_time") == want:
                return ZoomMeeting(str(d["id"]), d.get("join_url", ""), None,
                                   {"id": d["id"], "join_url": d.get("join_url"), "recovered": True}, False)
        return None

    def update_meeting(self, meeting_id, *, start, duration_minutes) -> None:
        request("PATCH", f"{self.API}/meetings/{meeting_id}", provider="zoom", headers=self._h(),
                json={"start_time": start.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "duration": duration_minutes})

    def delete_meeting(self, meeting_id) -> None:
        request("DELETE", f"{self.API}/meetings/{meeting_id}", provider="zoom", headers=self._h())

    def check(self) -> str:
        r = request("GET", f"{self.API}/users/{self.s.zoom_user_id}", provider="zoom", headers=self._h(), retries=1)
        return f"Zoom API ok for {r.json().get('email', 'user')}"


def verify_webhook(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    msg = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    try:
        fresh = abs(time.time() - int(timestamp) / (1000 if len(timestamp) > 10 else 1)) < 300
    except ValueError:
        return False
    return fresh and hmac.compare_digest(expected, signature)


def url_validation_response(plain_token: str, secret: str) -> dict:
    return {"plainToken": plain_token,
            "encryptedToken": hmac.new(secret.encode(), plain_token.encode(), hashlib.sha256).hexdigest()}


def get_zoom_provider() -> ZoomProvider:
    kind = get_settings().zoom_provider
    if kind == "mock":
        return MockZoomProvider()
    if kind == "zoom":
        return ServerToServerZoomProvider()
    raise NotConfiguredError(f"Unknown Zoom provider {kind!r}")


