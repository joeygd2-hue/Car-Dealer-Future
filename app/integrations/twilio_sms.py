"""Twilio Programmable Messaging (SMS).

Requires A2P 10DLC brand + campaign registration and documented consent before any texting.
Add Spanish opt-out keywords (ALTO, PARAR, CANCELAR, BAJA) in Twilio Advanced Opt-Out; the
application also recognizes them independently.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx

from app.integrations.base import FAILED, SENT, SendResult

PROVIDER = "TWILIO_SMS"


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def validate_signature(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature)


class TwilioSmsChannel:
    provider = PROVIDER
    supports_send = True

    def __init__(self, account_sid: str, auth_token: str, from_number: str, client: httpx.Client | None = None):
        self.sid = account_sid
        self.token = auth_token
        self.from_number = from_number
        self.client = client or httpx.Client(timeout=15)

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        try:
            resp = self.client.post(url, auth=(self.sid, self.token),
                                    data={"To": recipient_id, "From": self.from_number, "Body": text})
        except httpx.HTTPError as exc:
            return SendResult(FAILED, error=f"network error: {exc}", retryable=True)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code in (200, 201) and data.get("sid"):
            return SendResult(SENT, provider_message_id=data["sid"], raw=data)
        code = data.get("code")
        # 21610 = recipient replied STOP (unsubscribed) — permanent; caller must record opt-out.
        retryable = resp.status_code >= 500 or resp.status_code == 429
        return SendResult(FAILED, error=f"Twilio {resp.status_code} code={code}: {data.get('message', '')}",
                          retryable=retryable, raw=data)
