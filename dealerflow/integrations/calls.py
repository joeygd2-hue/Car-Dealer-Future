"""Human-operated click-to-call.

- ``manual``: the UI shows a tel: link; the person dials and logs the outcome. No provider.
- ``twilio``: Twilio rings the operator's own phone first; only after the operator answers does
  Twilio dial the prospect (one call, one human, no autodialing, no prerecorded voice,
  no recording). Caller ID is your own Twilio number or a Twilio-verified number.
- ``mock``: SIMULATED.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from xml.sax.saxutils import escape

from ..config import get_settings
from .errors import NotConfiguredError
from .http import request


@dataclass
class CallStart:
    provider_call_id: str | None
    simulated: bool
    tel_link: str | None = None


class CallProvider:
    name = "base"
    simulated = False

    def start_call(self, to_number: str, *, call_id: int) -> CallStart:
        raise NotImplementedError


class ManualCallProvider(CallProvider):
    name = "manual"

    def start_call(self, to_number, *, call_id) -> CallStart:
        return CallStart(None, False, tel_link=f"tel:{to_number}")


class MockCallProvider(CallProvider):
    name = "mock"
    simulated = True
    calls: list[str] = []

    def start_call(self, to_number, *, call_id) -> CallStart:
        type(self).calls.append(to_number)
        return CallStart(f"SIMCA{uuid.uuid4().hex[:16]}", True)


class TwilioCallProvider(CallProvider):
    name = "twilio"

    def __init__(self) -> None:
        s = get_settings()
        if not (s.twilio_account_sid and s.twilio_auth_token.get_secret_value() and s.twilio_caller_id and s.agent_phone):
            raise NotConfiguredError("Twilio is not configured (DF_TWILIO_ACCOUNT_SID / _AUTH_TOKEN / _CALLER_ID, DF_AGENT_PHONE)")
        self.s = s

    def start_call(self, to_number, *, call_id) -> CallStart:
        s = self.s
        # Leg 1 rings the operator. When they answer, this TwiML bridges to the prospect.
        twiml = (f'<Response><Say>Connecting your call.</Say><Dial callerId="{escape(s.twilio_caller_id)}" '
                 f'record="do-not-record"><Number>{escape(to_number)}</Number></Dial></Response>')
        r = request("POST", f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Calls.json",
                    provider="twilio", retries=0,
                    auth=(s.twilio_account_sid, s.twilio_auth_token.get_secret_value()),
                    data={"To": s.agent_phone, "From": s.twilio_caller_id, "Twiml": twiml,
                          "StatusCallback": f"{s.base_url}/webhooks/twilio/status?call_id={call_id}"})
        return CallStart(r.json().get("sid"), False)


def get_call_provider() -> CallProvider:
    kind = get_settings().call_provider
    return {"manual": ManualCallProvider, "mock": MockCallProvider, "twilio": TwilioCallProvider}[kind]()
