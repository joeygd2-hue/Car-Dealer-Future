"""Zoom Server-to-Server OAuth app (scope: meeting:write:meeting:admin or equivalent)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.integrations.base import IntegrationError, VideoMeeting


class ZoomMeetings:
    name = "zoom"
    simulated = False

    def __init__(self, account_id: str, client_id: str, client_secret: str, client: httpx.Client | None = None):
        self.account_id, self.client_id, self.client_secret = account_id, client_id, client_secret
        self.client = client or httpx.Client(timeout=15)

    def _token(self) -> str:
        resp = self.client.post("https://zoom.us/oauth/token",
                                params={"grant_type": "account_credentials", "account_id": self.account_id},
                                auth=(self.client_id, self.client_secret))
        if resp.status_code != 200:
            raise IntegrationError(f"Zoom token failed: {resp.status_code}", retryable=resp.status_code >= 500)
        return resp.json()["access_token"]

    def create_meeting(self, *, topic: str, start: datetime, duration_minutes: int) -> VideoMeeting:
        resp = self.client.post("https://api.zoom.us/v2/users/me/meetings",
                                headers={"Authorization": f"Bearer {self._token()}"},
                                json={"topic": topic, "type": 2, "duration": duration_minutes,
                                      "start_time": start.replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "settings": {"waiting_room": True}})
        if resp.status_code not in (200, 201):
            raise IntegrationError(f"Zoom create meeting {resp.status_code}: {resp.text[:200]}",
                                   retryable=resp.status_code >= 500 or resp.status_code == 429)
        data = resp.json()
        return VideoMeeting(str(data["id"]), data["join_url"])
