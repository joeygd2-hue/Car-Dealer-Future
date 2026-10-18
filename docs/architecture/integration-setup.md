# Integration setup

Every provider defaults to a SIMULATED mock. Switch one at a time, then use **Integrations → Verify
credentials**, which authenticates without sending anything. **None of the real integrations below
have been run against a live account yet** (no credentials were available while building Phase 1).
They are written against the providers' documented APIs and unit-tested with mocked HTTP. See
known-limitations.md.

## 1. Email

Choose one:

### A. SMTP + IMAP (simplest; works with Google Workspace or Microsoft 365 mailboxes that allow it)
1. Use a mailbox on your own domain (not a free Gmail address) with SPF, DKIM and DMARC published.
2. Google Workspace: turn on 2-Step Verification and create an app password, or use your provider's SMTP relay.
   Microsoft 365: SMTP AUTH must be enabled for the mailbox. Microsoft is retiring basic auth for SMTP AUTH, so prefer Gmail API or check Microsoft's current status.
3. Set `DF_EMAIL_PROVIDER=smtp`, `DF_SMTP_*` and `DF_IMAP_*`.
4. Replies are read over IMAP in read-only mode from `DF_IMAP_FOLDER`.

### B. Gmail API
1. Google Cloud console: create a project and enable the **Gmail API**.
2. OAuth consent screen: Internal (Workspace) if possible; External apps in testing mode issue refresh tokens that expire after 7 days.
3. Create an OAuth client ID (Desktop app). Run `python scripts/google_oauth.py CLIENT_ID CLIENT_SECRET` and approve.
4. Set `DF_EMAIL_PROVIDER=gmail`, `DF_GOOGLE_CLIENT_ID`, `DF_GOOGLE_CLIENT_SECRET`, `DF_GOOGLE_REFRESH_TOKEN`.
5. Scopes: `gmail.send` (sending) and `gmail.readonly` (reading replies). `gmail.readonly` is a restricted
   scope. For an External app used beyond your own test users, Google requires verification and a
   security assessment. An Internal Workspace app used only inside your own organisation avoids that.

Transactional providers (SendGrid, Postmark, Mailgun and similar) are **not** used for cold outreach.
Their acceptable-use policies restrict unsolicited or purchased-list email (see the feasibility report).

## 2. Google Calendar (availability + events)
1. Enable the **Google Calendar API** in the same project.
2. Re-run `scripts/google_oauth.py` (it requests `calendar.freebusy` and `calendar.events` by default, plus Gmail
   scopes unless `--no-gmail`).
3. Set `DF_CALENDAR_PROVIDER=google` and `DF_GOOGLE_CALENDAR_ID` (`primary` or a calendar's ID).
4. Availability comes from `POST /calendar/v3/freeBusy`. Events are created with `sendUpdates=all`, so the prospect
   receives Google's invitation email. Event IDs are deterministic per booking, so a retry cannot double-book.

## 3. Zoom
1. Zoom App Marketplace → Develop → Build App → **Server-to-Server OAuth** (an account admin must create or approve it).
2. Scopes: meeting write and read for your user (create/update/delete meetings), plus `user:read` for the
   credential check. Zoom's granular scope names are shown in the app's Scopes page. Grant only those.
3. Activate the app. Set `DF_ZOOM_PROVIDER=zoom`, `DF_ZOOM_ACCOUNT_ID`, `DF_ZOOM_CLIENT_ID`, `DF_ZOOM_CLIENT_SECRET`.
   `DF_ZOOM_USER_ID=me` creates meetings on the app owner's account.
4. Optional webhooks (meeting ended or deleted): add an Event Subscription with endpoint `https://<DF_BASE_URL>/webhooks/zoom`,
   events `meeting.ended` and `meeting.deleted`, and set `DF_ZOOM_WEBHOOK_SECRET_TOKEN` to the app's Secret Token.
   The endpoint answers Zoom's URL validation challenge and rejects unsigned or stale requests.
5. Meetings are created with a waiting room, no join-before-host and **no recording**.

## 4. Click-to-call (optional)
Default `DF_CALL_PROVIDER=manual`: the prospect page shows a `tel:` link, you dial from your own phone, then log the outcome.

Twilio (`DF_CALL_PROVIDER=twilio`):
1. Buy a Twilio number, or verify your existing business number as a caller ID. Set `DF_TWILIO_CALLER_ID`.
2. Set `DF_TWILIO_ACCOUNT_SID`, `DF_TWILIO_AUTH_TOKEN` and `DF_AGENT_PHONE` (your phone).
3. Clicking **Call** makes Twilio ring *your* phone first. When you answer, it connects you to the dealership.
   One call at a time, placed by a person. No autodialing, no prerecorded or AI voice, no recording, and caller ID
   is always your own number.
4. Status callbacks go to `/webhooks/twilio/status` and are verified with Twilio's request signature. Requires a public `DF_BASE_URL`.

Calls are blocked outside `DF_CALLING_HOURS_*` (local time) and for do-not-contact or suppressed numbers.

## 5. Lead discovery
- **Fixture** (default): SIMULATED, offline.
- **OpenStreetMap / Overpass**: no key. Results carry the ODbL attribution note. Keep request volume low on the public instance.
- **TxDMV dealer list (recommended)**: open https://texasdmv.my.salesforce-sites.com/dealers → *Motor Vehicle Dealers List*, download the spreadsheet, then **Import** it with source label "TxDMV dealer list YYYY-MM-DD" and that URL. Check the dry-run preview to confirm the columns mapped. License number, type, county and DBA are kept as evidence.
- **Google Places API**: not available as a lead source. Google's policies allow storing place IDs only, not business details. See the feasibility report.
- **CSV/XLSX import**: for lists you are allowed to use (for example a public licensing dataset). Always fill in the source.
- **Website research**: `DF_WEBSITE_RESEARCH_ENABLED=true`. Fetches at most the homepage and one inventory page per dealer,
  honours robots.txt, identifies itself with `DF_HTTP_USER_AGENT`, and stores observations as unverified evidence.

## 6. Public URL
Unsubscribe links, the one-click unsubscribe POST, and the Zoom and Twilio webhooks need `DF_BASE_URL` to be a public
HTTPS address (for example a reverse proxy such as Caddy or nginx in front of the `web` container).
`DF_SECRET_KEY` must stay stable, or unsubscribe links already sent stop working.
