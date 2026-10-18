"""Meta Messenger Platform (Facebook Page messaging).

Requires: Meta app with pages_messaging (+ App Review/Advanced Access for client Pages), Business
Verification, a Page access token per dealership Page, the app secret and a webhook verify token.
Marketplace buyer↔seller chats are NOT available through this API (see docs/research).
"""

from __future__ import annotations

import hashlib
import hmac

import httpx

from app.integrations.base import FAILED, SENT, SendResult

PROVIDER = "META_MESSENGER"


def verify_signature(app_secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """Validate ``X-Hub-Signature-256: sha256=<hex hmac>``."""
    if not app_secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))


def parse_webhook(payload: dict) -> list[dict]:
    """Normalize a Page webhook payload into inbound message events.

    Returns dicts: page_id, sender_id, mid, text, timestamp_ms. Echoes, reads and deliveries are skipped.
    """
    events = []
    if payload.get("object") != "page":
        return events
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id", ""))
        for item in entry.get("messaging", []):
            msg = item.get("message") or {}
            if not msg or msg.get("is_echo") or "mid" not in msg:
                continue
            events.append({
                "page_id": page_id,
                "sender_id": str(item.get("sender", {}).get("id", "")),
                "mid": msg["mid"],
                "text": msg.get("text") or ("[attachment]" if msg.get("attachments") else ""),
                "timestamp_ms": item.get("timestamp"),
            })
    return events


class MessengerChannel:
    provider = PROVIDER
    supports_send = True

    def __init__(self, page_id: str, page_access_token: str, graph_version: str = "v23.0",
                 client: httpx.Client | None = None):
        self.page_id = page_id
        self.token = page_access_token
        self.url = f"https://graph.facebook.com/{graph_version}/{page_id}/messages"
        self.client = client or httpx.Client(timeout=15)

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        body: dict = {"recipient": {"id": recipient_id}, "message": {"text": text}}
        if human_agent:  # human reply after the 24h window (requires Human Agent permission)
            body["messaging_type"] = "MESSAGE_TAG"
            body["tag"] = "HUMAN_AGENT"
        else:
            body["messaging_type"] = "RESPONSE"
        try:
            resp = self.client.post(self.url, params={"access_token": self.token}, json=body)
        except httpx.HTTPError as exc:
            return SendResult(FAILED, error=f"network error: {exc}", retryable=True)
        data = _json(resp)
        if resp.status_code == 200 and data.get("message_id"):
            return SendResult(SENT, provider_message_id=data["message_id"], raw=data)
        err = data.get("error", {})
        code = err.get("code")
        # 4/32/613 are rate limits; 1/2 transient; 5xx transient.
        retryable = resp.status_code >= 500 or resp.status_code == 429 or code in (1, 2, 4, 32, 613)
        return SendResult(FAILED, error=f"Meta API {resp.status_code}: {err.get('message', resp.text[:200])}",
                          retryable=retryable, raw=data)


def _json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
