# Known limitations, blockers and unfinished work

Last updated: 2026-10-18 (Phase 2 chat).

## Platform / provider blockers
- **Facebook Marketplace has no API for dealer vehicle listings.** Publication, edits, deletion and buyer messages
  on Marketplace are manual. The app prepares packages and records staff-attested results; it cannot verify that a
  Marketplace listing is still live or detect edits made on Facebook. Browser automation is deliberately not used.
- **Meta catalog (Automotive Inventory Ads) not verified live.** Needs a Meta app with `catalog_management` +
  `business_management` (App Review), Business Verification, a dealer Business Manager system-user token, a vehicle
  catalog and vehicle detail page URLs. The `check_batch_request_status` response shape is handled defensively but
  unverified. Catalog items power paid ads; they are not organic Marketplace listings.
- **Gmail / Google Calendar not verified live.** Needs an OAuth client, consent screen and refresh token. Reading the
  inbox needs the Restricted `gmail.readonly` scope (verification + annual security assessment unless the OAuth app
  is Internal to the agency's Workspace). Inbox monitoring polls; Pub/Sub push (`users.watch`) is not implemented.
  Microsoft 365, Zoom and Teams are not implemented.
- **Google Places not verified live.** Needs an API key and billing. ToS limits storage: only `place_id`
  indefinitely; the purge job clears other Places content after 30 days, but business name/website copied into the
  CRM should be reviewed against the Places terms before production use (prefer TxDMV/dealer-website data).
- **ESPs (SendGrid, Postmark, Resend, SES) cannot be used for cold outreach** under their policies.
- **SMS and voice outreach are disabled** (TCPA consent, Texas Bus. & Com. Code ch. 302 registration, SB 140).
  Enabling them requires documented prior express written consent capture and legal sign-off.
- **LinkedIn automation is prohibited** by its User Agreement and is not implemented.
- No DMS vendor presets: generic CSV/XLSX mapping only until real sample exports are available. No SFTP receiver
  for HomeNet/vAuto-style feed drops.

## Missing credentials / configuration
`ANTHROPIC_API_KEY` (AI generation and classification are off without it), Meta app + tokens, Google OAuth client
+ refresh token, Places API key, a public `PUBLIC_BASE_URL` for unsubscribe links, a real postal address and
sender domain with SPF/DKIM/DMARC, production `APP_ENCRYPTION_KEY` and `UNSUBSCRIBE_SECRET`.

## Functional gaps
- Template generator cannot apply free-text reviewer feedback (it records it); AI generation or manual edits needed.
- Spanish translations use a fixed vocabulary; unknown features/colours stay in English and are flagged.
- Photos are referenced by URL only: no download, resizing, reachability check or image moderation.
- No VIN decoding (NHTSA vPIC) yet; only offline check-digit/model-year validation.
- Listing views/inquiries/sales are not measured (no reliable source).
- Outreach: meeting slot parsing understands option numbers/ordinals/exact labels only; other replies escalate.
  Referred contacts require manual verification. No reschedule/cancel flow for booked meetings. Email deliverability
  monitoring (spam rate, DMARC reports) is not implemented. Cost of email/Places is estimated; AI cost is measured.
- No password reset, MFA, user-invite UI or rate limiting on login (Phase 4 security work).
- Tenant isolation relies on app-level ORM scoping + composite FKs; PostgreSQL row-level security is not configured.
- Worker uses one job at a time per process; scale by running more worker processes (leases make this safe).
- SQLite is for development/tests. Use PostgreSQL in production (`psycopg` extra); Postgres has not been exercised in tests.

## Legal review recommended before live outreach
CAN-SPAM controls are implemented (postal address, opt-out link + reply handling, no deceptive subjects), but the
sender identity, physical address, campaign copy, approved company facts and Places/TxDMV data use should be
reviewed by counsel. Texas ch. 304/302 apply to calls/texts, which remain disabled.
