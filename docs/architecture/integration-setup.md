# Integration setup (exact steps)

Every provider defaults to a SIMULATED mock. Configure one at a time, verify it, and only then
move on. **None of these has been run against a live account yet.** See
`docs/audit/integration-verification-matrix.md` for what is and isn't verified. Requirements below
were checked against provider documentation on 2026-10-18.

Keep secrets out of `.env`: put each one in a file under `./secrets/` named like the variable in
lower case (e.g. `secrets/df_smtp_password`), then `chown 10001:10001 secrets/* && chmod 400 secrets/*`,
and list it under `secrets:` in `docker-compose.yml`. Locally, `DF_SECRETS_DIR=./secrets` works too.

## 0. Prerequisites
1. A domain you control for sending, e.g. `yourcompany.com`, with a Google Workspace mailbox on it (recommended).
2. A public HTTPS URL for the app (`DF_BASE_URL`): unsubscribe links and webhooks need it.
3. `DF_BUSINESS_NAME`, `DF_BUSINESS_POSTAL_ADDRESS` (a real street address or P.O. box), `DF_SENDER_NAME`, `DF_SENDER_EMAIL`.
4. `DF_TEST_INBOXES`: one or more inboxes **you** control (e.g. `you@yourcompany.com,yourpersonal@gmail.com`).

## 1. Sender authentication (DNS)
1. **SPF:** TXT record on `yourcompany.com`: `v=spf1 include:_spf.google.com ~all` (Workspace).
2. **DKIM:** Google Admin → Apps → Google Workspace → Gmail → Authenticate email → Generate new record (2048-bit) → publish the TXT record → **Start authentication**.
3. **DMARC:** TXT record on `_dmarc.yourcompany.com`: `v=DMARC1; p=none; rua=mailto:dmarc@yourcompany.com` (move to `quarantine` after a few clean weeks).
4. Check with the live-check email: in Gmail use ⋮ → *Show original*. SPF, DKIM and DMARC should all show **PASS**.

## 2. Email: option A, Gmail API (recommended with Workspace)
1. console.cloud.google.com → create a project → APIs & Services → **Enable** "Gmail API" (and "Google Calendar API" for step 4).
2. OAuth consent screen → User type **Internal**. This avoids Google's restricted-scope verification for `gmail.readonly`, which is required for External apps.
3. Credentials → Create OAuth client ID → **Desktop app**. Note the client ID and secret.
4. On your computer: `python scripts/google_oauth.py CLIENT_ID CLIENT_SECRET`. Sign in **as the sending mailbox**, approve, and paste the code.
   It requests exactly `gmail.send`, `gmail.readonly`, `calendar.freebusy` and `calendar.events`.
5. Settings: `DF_EMAIL_PROVIDER=gmail`, `DF_GOOGLE_CLIENT_ID`; secrets `df_google_client_secret`, `df_google_refresh_token`.
6. Limits: Workspace sends up to 2,000/day (500 on trial). The app's cap is `DF_OUTREACH_DAILY_CAP=25`.

## 2b. Email: option B, SMTP + IMAP
1. Workspace: enable 2-Step Verification for the mailbox, then create an **app password** (myaccount.google.com → Security → App passwords).
2. Settings: `DF_EMAIL_PROVIDER=smtp`, `DF_SMTP_HOST=smtp.gmail.com`, `DF_SMTP_PORT=587`, `DF_SMTP_USERNAME=<mailbox>`,
   `DF_IMAP_HOST=imap.gmail.com`, `DF_IMAP_USERNAME=<mailbox>`; secrets `df_smtp_password`, `df_imap_password` (the app password).
3. IMAP is read-only (messages are not marked read).

**Do not use SendGrid, Mailgun or similar for cold outreach.** Their policies require prior affirmative consent.

## 3. Google Calendar
Uses the same OAuth client and refresh token as step 2. Set `DF_CALENDAR_PROVIDER=google` and `DF_GOOGLE_CALENDAR_ID=primary`
(or the calendar's ID from Calendar settings → Integrate calendar).
- Availability: `freeBusy` (scope `calendar.freebusy`).
- Booking: `events.insert` with `sendUpdates=all` (scope `calendar.events`), so the prospect gets Google's invitation.
- Working hours for suggestions: `DF_WORKING_HOURS_START/END`, `DF_WORKING_DAYS`, `DF_TIMEZONE`.

## 4. Zoom (Server-to-Server OAuth)
1. marketplace.zoom.us → Develop → Build App → **Server-to-Server OAuth App**. A Zoom account admin must create it, or grant the role permission to.
2. App credentials: copy the Account ID, Client ID and Client Secret.
3. Scopes: add only `meeting:write:meeting:admin`, `meeting:update:meeting:admin`, `meeting:delete:meeting:admin`,
   `meeting:read:list_meetings:admin` and `user:read:user:admin`. *(Names from secondary sources: confirm on the Scopes page.)*
4. **Activate** the app.
5. Settings: `DF_ZOOM_PROVIDER=zoom`, `DF_ZOOM_ACCOUNT_ID`, `DF_ZOOM_CLIENT_ID`, `DF_ZOOM_USER_ID=<host email>`; secret `df_zoom_client_secret`.
6. Optional webhooks: Feature → Event Subscriptions → endpoint `https://<DF_BASE_URL>/webhooks/zoom`, events `meeting.ended` and `meeting.deleted`.
   Secret `df_zoom_webhook_secret_token` = the app's Secret Token.
7. Limit: 100 meeting creates/updates per host per day.

## 5. Click-to-call (optional; no automated calls)
Default `DF_CALL_PROVIDER=manual`: a `tel:` link, then you log the outcome. Twilio (`twilio`): an agent-first bridge with your own or a
verified caller ID and no recording. Use a restricted API key. **Before a regular cold-calling routine, resolve the Texas Chapter 302
question** (`docs/audit/compliance-checklist.md` C5).

## 6. Lead data
TxDMV list: see `docs/audit/lead-sourcing.md`. Google Places is not usable for stored leads.

## 7. Controlled live verification (do this before authorizing live operation)
Nothing here contacts a prospect. Messages go only to `DF_TEST_INBOXES`.
1. `python -m dealerflow check-integrations`, or Integrations → **Verify credentials**. All should report OK.
2. `python -m dealerflow live-check send --to you@yourcompany.com --i-control-this-inbox`
   → expect `message_status: sent` with a provider message ID. Check SPF, DKIM and DMARC in *Show original*.
3. In that inbox: **reply** "Sounds good, can we set up a Zoom call next week?", then click the email's **unsubscribe** link.
4. `python -m dealerflow live-check inbox` → expect the reply `matched_by: in_reply_to` (or `thread`), classified
   `meeting_request`, `suppressed: True`, `stage: do_not_contact`.
5. Bounce: add `nobody-xyz@yourcompany.com` (a mailbox that doesn't exist) to `DF_TEST_INBOXES`, run `live-check send` to it, wait
   a few minutes, then run `live-check inbox`. The address should be suppressed with reason `bounce`.
6. `python -m dealerflow live-check meeting` → reports busy blocks, then creates and deletes one Zoom meeting and one calendar event
   30 days out. Confirm both disappear in Zoom and Calendar.
7. Revocation: revoke the app in your Google account (Security → Third-party access), run `live-check inbox`, and expect an
   auth error. Integrations should show the provider **disabled**. Re-authorize, then **Verify credentials** re-enables it.
8. Record the results in `docs/audit/integration-verification-matrix.md`.

Only after all of that: set `DF_OUTREACH_SENDING_ENABLED=true`, restart, and have an admin authorize live operation (Agent page).
