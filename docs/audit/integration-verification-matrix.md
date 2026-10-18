# Integration verification matrix (2026-10-18)

**No integration has been verified against a live account.** No credentials were available in the
audit environment, and you asked that nothing be sent or called. Everything below is checked against mocks
or against mocked HTTP that follows each provider's documented API. Live verification uses
`python -m dealerflow live-check …` (see `docs/architecture/integration-setup.md` §7) once you supply
credentials and controlled inboxes.

Status: **Verified** (tested for real), **Partially verified** (code and mocked tests pass; live run pending),
**Unverified**, **Blocked** (needs something from the owner).

| Check (pre-live requirement) | Email: Gmail API | Email: SMTP+IMAP | Google Calendar | Zoom S2S | How it will be verified live |
|---|---|---|---|---|---|
| Credentials accepted (no send) | Partially verified | Partially verified | Partially verified | Partially verified | Integrations → **Verify credentials** |
| Delivery to a controlled inbox | Blocked: needs credentials + `DF_TEST_INBOXES` | Blocked: same | — | — | `live-check send --to … --i-control-this-inbox` |
| Sender authentication (SPF/DKIM/DMARC pass) | Blocked: needs your domain DNS | Blocked: same | — | — | Gmail "Show original" on the test email |
| Unsubscribe link and one-click header processed | Partially verified (mock + header test) | Partially verified | — | — | Click the link in the test email; `live-check inbox` shows the suppression |
| Reply import and matching to the right prospect | Partially verified (mocked Gmail raw parse) | Partially verified (IMAP untested) | — | — | Reply to the test email; `live-check inbox` shows `matched_by` |
| Sequence cancellation on reply | Verified (mock) | Verified (mock) | — | — | Same live run: enrollment stops |
| Bounce handling and failure reporting | Partially verified (DSN parser tested with a synthetic DSN) | Partially verified | — | — | Send to `bounce-test@<your domain>` (a non-existent mailbox) via live-check; check suppression |
| Availability (free/busy) | — | — | Partially verified (mocked) | — | `live-check meeting` reports busy blocks |
| Create, then delete, meeting/event | — | — | Partially verified | Partially verified | `live-check meeting` creates and deletes one 15-min item 30 days out |
| Duplicate-safe retries | — | — | Verified (deterministic event IDs; IDs are valid base32hex) | Verified (mock lost-response recovery) | — |
| Revoked credentials disable the integration safely | Verified (mock) | Verified (mock) | Verified (mock) | Verified (mock) | Revoke the grant, run a job: the provider is marked disabled and no further calls are made |
| Rate limits respected | Gmail: 6,000 quota units/min/user, `messages.send` = 100 units → about 60 sends/min [verified docs]; app cap 25/day | Mailbox limits (Workspace 2,000/day) | Not a constraint at pilot volume | 100 meeting creates/updates per host per day (community-reported) [secondary] | — |

## Verified provider requirements (docs read 2026-10-18)

| Item | Requirement | Source | Confidence |
|---|---|---|---|
| Gmail scopes | `gmail.send` (sensitive), `gmail.readonly` (restricted: verification plus a security assessment if External) | developers.google.com/workspace/gmail/api/auth/scopes | Primary |
| Gmail quota | 6,000 units/min per user; send = 100, get(raw) = 60, list = 5 | developers.google.com/workspace/gmail/api/reference/quota | Primary |
| Workspace sending limits | 2,000 msgs/day per user (500 on trial); 3,000 external recipients/day | knowledge.workspace.google.com (Gmail sending limits) | Primary |
| Sender rules | SPF or DKIM for all senders; DMARC and one-click unsubscribe for bulk senders; spam rate < 0.1% | support.google.com/a/answer/81126 | Primary |
| Calendar freeBusy scopes | `calendar.freebusy` (or `calendar.events.freebusy`, `calendar.readonly`, `calendar`) | developers.google.com/workspace/calendar/api/v3/reference/freebusy/query | Primary |
| Calendar events.insert scopes | `calendar.events` (or `calendar.events.owned`, `calendar.app.created`, `calendar`). Client IDs: base32hex `[a-v0-9]`, 5–1024 chars | …/events/insert | Primary |
| Zoom S2S token | `POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=…`, Basic auth; 1-hour token, no refresh token | developers.zoom.us/docs/internal-apps/s2s-oauth | Primary |
| Zoom scopes | `meeting:write:meeting:admin`, `meeting:update:meeting:admin`, `meeting:delete:meeting:admin`, `meeting:read:list_meetings:admin` (lost-response recovery), `user:read:user:admin` (credential check) | Zoom community and integration docs | **Secondary: confirm on your app's Scopes page** |
| Zoom limits | 100 meeting create/update requests per host per day, reset at 00:00 UTC | devforum.zoom.us | Secondary |
| Transactional ESPs | Twilio SendGrid forbids lists without affirmative consent, including info@-type aliases; Mailgun forbids bought or scraped lists | twilio.com/legal/service-country-specific-terms/email | Primary |
