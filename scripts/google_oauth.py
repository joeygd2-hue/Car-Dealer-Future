"""One-time helper: obtain a Google OAuth refresh token for Gmail + Calendar.

Usage (on your own machine):
  1. Google Cloud console -> APIs & Services -> enable Gmail API and Google Calendar API.
  2. Create an OAuth client ID of type "Desktop app"; note the client ID and secret.
  3. python scripts/google_oauth.py CLIENT_ID CLIENT_SECRET [--no-gmail] [--no-calendar]
  4. Open the printed URL, approve, paste the code shown back here.
  5. Put the printed refresh token in DF_GOOGLE_REFRESH_TOKEN (never commit it).

Scopes requested (minimum needed):
  gmail.send        send approved outreach
  gmail.readonly    read replies (Google classifies this as a *restricted* scope; see docs)
  calendar.freebusy read availability without event details
  calendar.events   create/update/delete the meeting events this app books
"""

from __future__ import annotations

import json
import secrets
import sys
import urllib.parse
import urllib.request

SCOPES = {
    "gmail": ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"],
    "calendar": ["https://www.googleapis.com/auth/calendar.freebusy", "https://www.googleapis.com/auth/calendar.events"],
}


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    client_id, secret = sys.argv[1], sys.argv[2]
    scopes = []
    if "--no-gmail" not in sys.argv:
        scopes += SCOPES["gmail"]
    if "--no-calendar" not in sys.argv:
        scopes += SCOPES["calendar"]
    # Loopback redirect: Google shows an error page after approval; copy the "code" value from its URL.
    redirect = "http://127.0.0.1:8085/"
    state = secrets.token_urlsafe(8)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(scopes),
        "access_type": "offline", "prompt": "consent", "state": state})
    print("Open this URL, approve, then copy the 'code' parameter from the address bar:\n\n" + url + "\n")
    code = input("code: ").strip()
    data = urllib.parse.urlencode({"code": code, "client_id": client_id, "client_secret": secret,
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data) as r:
        body = json.load(r)
    print("\nDF_GOOGLE_REFRESH_TOKEN=" + body["refresh_token"])
    print("granted scopes:", body.get("scope"))


if __name__ == "__main__":
    main()
