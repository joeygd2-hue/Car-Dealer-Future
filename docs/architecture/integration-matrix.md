# Integration matrix

Status key: **Mock-tested** = exercised end-to-end with a SIMULATED provider. **HTTP-tested** = request and
response handling checked against mocked HTTP that follows the provider's documented API. **Live-verified** =
called against a real account. *Nothing in Phase 1 is live-verified yet.*

Policy and legal sources for each row are in `docs/research/source-register.md` (IDs in brackets).

| Capability | Provider (setting) | Auth / minimum permissions | Used for | Status | Allowed use / constraints | Cost |
|---|---|---|---|---|---|---|
| Lead discovery | Fixture (`fixture`) | none | Offline demo and tests | Mock-tested | SIMULATED, fictional data | free |
| Lead discovery | OpenStreetMap Overpass (`osm`) | none | `shop=car` within radius | HTTP-tested | ODbL: attribution required; derived databases must stay under ODbL terms; light use of the public endpoint [S-OSM] | free |
| Lead discovery | Google Places API (New) Text Search (`google_places`) | API key restricted to Places API + server IP | Name, address, phone, website, location, place ID | HTTP-tested | Caching of most Places content is restricted; place IDs may be stored. Raw payloads purged after 30 days. Show Google attribution where Places data is displayed [S-GPT] | Pay-per-request; see report |
| Lead discovery | Dealer websites (`DF_WEBSITE_RESEARCH_ENABLED`) | none | Public homepage + one inventory page | HTTP-tested | robots.txt honoured, identifying user agent, ≤2 pages per site, no logins or forms, no CAPTCHA bypass [S-ROBOTS] | free |
| Lead discovery | CSV / XLSX import | n/a | Lists you're entitled to use (e.g. a public license list) | Mock-tested | Must record source; operator is responsible for list rights | — |
| Send email | Mock (`mock`) | n/a | Default | Mock-tested | SIMULATED | free |
| Send + read email | SMTP + IMAP (`smtp`) | Mailbox credentials (app password / relay); IMAP read-only | Outreach, replies | HTTP-tested (fake SMTP); IMAP untested | Mailbox provider's sending limits and anti-spam rules; SPF/DKIM/DMARC [S-GBULK] | mailbox plan |
| Send + read email | Gmail API (`gmail`) | OAuth: `gmail.send`, `gmail.readonly` (restricted scope) | Outreach, replies, threading | HTTP-tested | Workspace/consumer daily sending limits; restricted-scope verification for External apps [S-GMAIL] | Workspace plan |
| Email (rejected) | SendGrid / Postmark / Mailgun etc. | — | Not used for cold outreach | — | AUPs restrict unsolicited/purchased-list email [S-ESP] | — |
| Availability | Google Calendar (`google`) | OAuth: `calendar.freebusy` | Free/busy before suggesting times | HTTP-tested | — [S-GCAL] | free with account |
| Calendar events | Google Calendar (`google`) | OAuth: `calendar.events` | Create/update/delete the booked event, invite attendee | HTTP-tested | Deterministic event IDs prevent duplicates | free with account |
| Video meetings | Zoom Server-to-Server OAuth (`zoom`) | Account credentials grant; meeting read/write scopes for one user; `user:read` for the check | Create/update/delete meetings | HTTP-tested | Rate limits by plan; JWT apps are deprecated [S-ZOOM] | Zoom plan |
| Meeting events | Zoom webhooks | Secret token (HMAC-SHA256 signature) | meeting.ended / meeting.deleted | Signature tested | Public HTTPS endpoint needed | — |
| Calls | Manual (`manual`) | none | tel: link + outcome log | Mock-tested | Human dials; calling-hours check | — |
| Calls | Twilio (`twilio`) | Account SID + auth token; owned/verified caller ID | Agent-first click-to-call bridge | HTTP-tested | No autodialer, no prerecorded/AI voice, no recording, accurate caller ID [S-TCPA][S-TX304] | per-minute |
| Notifications | Configured email provider | as above | Daily digest to `DF_NOTIFY_EMAIL` | Mock-tested | Internal email | — |

## Explicitly not built

| Option | Reason |
|---|---|
| Automated or prerecorded/AI-voice calling, voice drops | TCPA restrictions on artificial or prerecorded voice; risk with business cell phones [S-TCPA][S-FCC-AI] |
| SMS / text outreach | Texas SB 140 (2025) brought texts into the telephone-solicitation rules; consent and registration questions unresolved [S-TXSB140] |
| Scraping Google Maps, Yelp or social sites | Their terms prohibit it; official APIs only |
| Buying email lists | ESP and mailbox-provider policies; deliverability and legal risk |
| Microsoft Graph | Not needed for a Google Workspace setup; SMTP/IMAP covers M365 where allowed. Candidate for a later phase |
| Open/click tracking | Not needed; avoids tracking pixels |
