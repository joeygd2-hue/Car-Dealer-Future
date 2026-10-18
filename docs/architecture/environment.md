# Environment variables

All settings are read from the environment (prefix `DF_`) or a `.env` file in the working directory
(`dealerflow/config.py`). Secrets are only ever read from the environment: they are never written to
the database, never rendered into pages, never logged (the JSON log formatter redacts keys containing
password/secret/token), and never placed in any AI prompt. The app makes no LLM calls in Phase 1.

In production, inject secrets from your host's secret store (Docker/Compose secrets, systemd
`LoadCredential`, a cloud secret manager) rather than a plain `.env` file. **The app does not encrypt
secrets at rest itself.** See known-limitations.md.

## Core

| Variable | Default | Meaning |
|---|---|---|
| `DF_ENV` | `development` | `development`, `test` or `production`. Production requires `DF_SECRET_KEY`, sets secure cookies and hides `/api/docs`. |
| `DF_SECRET_KEY` | — | Signs sessions and unsubscribe tokens. **Required in production.** Changing it breaks unsubscribe links already sent. |
| `DF_DATABASE_URL` | `sqlite:///./state/dealerflow.db` | SQLAlchemy URL. Postgres: `postgresql+psycopg://user:pass@host/db` after `pip install -e ".[postgres]"`. The full test suite passes on SQLite and on Postgres 16 (`DF_TEST_PG_URL=… pytest`). |
| `DF_BASE_URL` | `http://127.0.0.1:8000` | Public HTTPS URL. Used in unsubscribe links, Twilio callbacks and Zoom webhooks. |
| `DF_WEB_HOST` / `DF_WEB_PORT` | `127.0.0.1` / `8000` | Bind address for `python -m dealerflow web`. |
| `DF_LOG_LEVEL` | `INFO` | JSON-lines logs to stdout. |
| `DF_TIMEZONE` | `America/Chicago` | Display, operating hours, calling hours and daily report boundaries. |
| `DF_SESSION_MAX_AGE_SECONDS` | `43200` | Login session lifetime. |

## Sender identity (required before any real email)

| Variable | Meaning |
|---|---|
| `DF_BUSINESS_NAME` | Legal or trade name shown in the footer. |
| `DF_BUSINESS_POSTAL_ADDRESS` | A valid physical postal address (street, P.O. box, or a registered private mailbox). CAN-SPAM requires it. |
| `DF_SENDER_NAME` / `DF_SENDER_EMAIL` | From name and address. The address must be a mailbox you control on a domain with SPF, DKIM and DMARC. |

The compliance gate blocks every send while any of these are empty.

## Target market

| Variable | Default | Meaning |
|---|---|---|
| `DF_TARGET_LAT` / `DF_TARGET_LNG` | `29.6197` / `-95.6349` | Centre point (Sugar Land City Hall area) for geographic scoring and searches. |
| `DF_TARGET_RADIUS_MILES` | `30` | Full credit within half the radius, partial credit up to the radius. |
| `DF_TARGET_CITIES` | Sugar Land, Stafford, Missouri City, Richmond, Rosenberg, Houston, Katy, Pearland, Fulshear, Meadows Place | Used when a prospect has no coordinates. |

## Providers

| Variable | Default | Values |
|---|---|---|
| `DF_EMAIL_PROVIDER` | `mock` | `mock` (SIMULATED), `smtp` (SMTP send + IMAP read), `gmail` (Gmail API) |
| `DF_CALENDAR_PROVIDER` | `mock` | `mock`, `google` |
| `DF_ZOOM_PROVIDER` | `mock` | `mock`, `zoom` |
| `DF_CALL_PROVIDER` | `manual` | `manual` (tel: link + outcome logging), `twilio`, `mock` |

## Outreach limits

| Variable | Default | Meaning |
|---|---|---|
| `DF_OUTREACH_SENDING_ENABLED` | `false` | Master switch for real providers. Real sends also need admin live authorization (Agent page). |
| `DF_OUTREACH_DAILY_CAP` | `25` | Max real emails per UTC day across the system; extra sends are deferred to the next day. |
| `DF_OUTREACH_PER_PROSPECT_COOLDOWN_DAYS` | `7` | Minimum gap between cold messages to one prospect. |
| `DF_OUTREACH_MAX_TOUCHES_PER_PROSPECT` | `3` | Max cold messages to one prospect without a reply. |

Agent limits (tick interval, operating hours, per-tick caps, budgets, discovery source) are edited
by an admin on the Agent page and stored in the `agent_state` row. See autonomous-agent.md.

## SMTP / IMAP (`DF_EMAIL_PROVIDER=smtp`)

`DF_SMTP_HOST`, `DF_SMTP_PORT` (587), `DF_SMTP_USERNAME`, `DF_SMTP_PASSWORD`, `DF_SMTP_STARTTLS` (true),
`DF_IMAP_HOST`, `DF_IMAP_PORT` (993), `DF_IMAP_USERNAME`, `DF_IMAP_PASSWORD`, `DF_IMAP_FOLDER` (INBOX).
IMAP is read-only (`BODY.PEEK`); messages are not marked read or moved.

## Google (Gmail API and/or Calendar)

`DF_GOOGLE_CLIENT_ID`, `DF_GOOGLE_CLIENT_SECRET`, `DF_GOOGLE_REFRESH_TOKEN` (from
`scripts/google_oauth.py`), `DF_GOOGLE_CALENDAR_ID` (`primary`), `DF_GMAIL_USER` (`me`).

## Zoom (`DF_ZOOM_PROVIDER=zoom`)

`DF_ZOOM_ACCOUNT_ID`, `DF_ZOOM_CLIENT_ID`, `DF_ZOOM_CLIENT_SECRET` (Server-to-Server OAuth app),
`DF_ZOOM_USER_ID` (`me` = the app owner), `DF_ZOOM_WEBHOOK_SECRET_TOKEN` (optional; enables `/webhooks/zoom`).

## Calls (`DF_CALL_PROVIDER=twilio`)

`DF_TWILIO_ACCOUNT_SID`, `DF_TWILIO_AUTH_TOKEN`, `DF_TWILIO_CALLER_ID` (a Twilio number or verified
caller ID you own), `DF_AGENT_PHONE` (the phone Twilio rings first).
`DF_CALLING_HOURS_START` (9), `DF_CALLING_HOURS_END` (21), `DF_CALLING_SUNDAY_START` (12) — local time.

## Lead discovery

| Variable | Default | Meaning |
|---|---|---|
| `DF_GOOGLE_PLACES_API_KEY` | — | Not used for lead discovery (Places results may not be stored). Reserved for a later live-lookup feature. |
| `DF_OVERPASS_URL` | public overpass-api.de | Use your own Overpass instance for heavy use. |
| `DF_WEBSITE_RESEARCH_ENABLED` | `false` | Allow fetching dealers' own public homepages (robots.txt honoured). |
| `DF_HTTP_USER_AGENT` | `dealerflow-research/0.1 …` | Identify yourself and give a contact address. |

## Meetings and notifications

`DF_MEETING_LENGTH_MINUTES` (30), `DF_MEETING_BUFFER_MINUTES` (15), `DF_WORKING_HOURS_START` (9),
`DF_WORKING_HOURS_END` (17), `DF_WORKING_DAYS` (`0,1,2,3,4`, Monday=0).
`DF_NOTIFY_EMAIL`: optional daily digest recipient (after 07:00 local, once per day). Leave empty to disable.

## Phase 4: clients, finance, SEO, reporting

| Variable | Default | Meaning |
|---|---|---|
| `DF_CLIENT_EMAIL_SENDING_ENABLED` | `false` | Allow real email to client dealerships (monthly reports, lead summaries). Also requires a real email provider and, per client, a `report_email` connection marked connected. Otherwise messages are SIMULATED (mock) or not sent. |
| `DF_ATTACHMENTS_DIR` | `./state/attachments` (`/data/attachments` in Docker) | Expense receipts. Back it up; use an encrypted volume. |
| `DF_MAX_UPLOAD_BYTES` | `10485760` | Upload limit (PDF, PNG, JPEG, WEBP, CSV, TXT only). |
| `DF_API_RATE_LIMIT_PER_MINUTE` | `300` | Per-IP limit for `/api/*` and `/webhooks/*` (per process). |
| `DF_CRAWLER_TIMEOUT_SECONDS` | `15` | SEO crawler request timeout. The crawler uses `DF_HTTP_USER_AGENT`, honours robots.txt, waits ≥0.5 s (site setting, default 1 s) and stops on HTTP 429. |
| `DF_PAGESPEED_API_KEY` | — | Optional Google PageSpeed Insights API key for mobile performance in SEO audits. |
| `DF_STRIPE_WEBHOOK_SECRET` | — | `whsec_…` signing secret for `POST /webhooks/stripe`. Empty = endpoint rejects everything. Use a test-mode endpoint first. |
| `DF_AI_ENABLED` | `false` | Optional Claude API use (`pip install -e ".[ai]"`). Nothing in Phase 4 requires it. |
| `DF_ANTHROPIC_API_KEY` | — | Claude API key (server-side only). |
| `DF_AI_MODEL` | `claude-opus-5-5` | Model id; per-token estimate rates are in Settings → cost estimates. |
| `DF_RETENTION_DAYS_CONVERSATIONS` | `365` | Car-buyer conversation PII older than this is purged daily. |

In production (`DF_ENV=production`) the app refuses to start if `DF_SECRET_KEY` is shorter than 32 characters,
or if `DF_BASE_URL` is not https while `DF_OUTREACH_SENDING_ENABLED` or `DF_CLIENT_EMAIL_SENDING_ENABLED` is true.
It logs a warning for SQLite or a non-https base URL.
