# Known limitations (Phases 1, 1.5 audit, 2, 3 and 4)

Last updated 2026-10-18 (after the launch-readiness audit; see `docs/audit/launch-readiness-audit.md` for
blockers and priorities). Be explicit with anyone relying on this system: **no real email, calendar, Zoom,
Twilio, Google Places or website request has been made by this code yet.** Every end-to-end test ran against
SIMULATED providers or mocked HTTP. The one real external fetch was a manual download of the public TxDMV dealer list.
Controlled live verification is ready to run: `python -m dealerflow live-check` (integration-setup §7).

## Not verified against live services (credentials needed)

| Integration | Status | What's needed to verify |
|---|---|---|
| Gmail API send/read | Code + mocked-HTTP test only | Google Cloud project, OAuth client, refresh token (`scripts/google_oauth.py`); then Integrations → Verify, then one human-approved send to your own address |
| SMTP/IMAP | Code + fake-SMTP test only; IMAP fetch untested | Mailbox credentials (app password or relay) |
| Google Calendar free/busy + events | Code + mocked-HTTP test only | Same Google project with Calendar API + scopes |
| Zoom Server-to-Server OAuth | Code + mocked-HTTP test only | Zoom S2S app (account admin) with meeting scopes |
| Zoom webhooks | Signature/validation tested locally | Public HTTPS `DF_BASE_URL` + event subscription |
| Twilio click-to-call | Code + mocked-HTTP test only; status webhook untested | Twilio account, number or verified caller ID |
| Google Places (New) | **Disabled as a lead source** (terms allow storing place IDs only); client code kept | A live, non-stored lookup feature in a later phase |
| OpenStreetMap Overpass | Code + mocked-HTTP test only | None (public), but not called during the build |
| Website research | Mocked HTTP only; off by default | Enable and try on a few dealer sites |
| Microsoft 365 / Graph mail and calendar | **Not implemented** | Use SMTP/IMAP if your tenant allows it, or add a Graph provider |
| Bounce/delivery webhooks | **Not implemented.** Bounces are detected from bounce emails in the inbox (matched by quoted Message-ID or DSN Final-Recipient, tested with a synthetic DSN). `delivered` is never set (SMTP/Gmail don't report it) | `live-check` bounce step |

## Functional gaps

- **Reply classification is rule-based** (English/Spanish keyword patterns). It handles the common cases in
  `tests/test_classify.py` but will misread unusual phrasing. Low-confidence results are escalated to a person.
  Classifications can be corrected in the inbox.
- **Auto-answers are keyword-matched** to approved knowledge items. There's no language model drafting. The agent
  never writes free text of its own; it only sends approved text.
- **Inventory activity** is a rough lower bound: distinct "year make" strings on the first inventory page.
  Many dealer sites render inventory with JavaScript, so this will often read 0 or miss the page.
- **Dealership type** comes from TxDMV `DealerType` (franchise, or blank → independent, marked unverified) or OSM tags;
  other leads stay `unknown` until a person sets it.
- **No automatic TxDMV download.** The `.xls` is downloaded by hand and imported with the TxDMV profile (column layout
  verified against the 2026-10-18 file). If TxDMV changes the columns, the import refuses the file rather than guessing.
- **Only business-domain emails are used for outreach.** In Fort Bend that's 33 of 191 dealers; the rest need a person
  to verify a contact first. Scores on TxDMV-only data top out around 58 until websites are researched.
- **Meeting time selection is manual.** The app suggests real free slots, but a person copies them into the reply and
  records which one the prospect accepted. It does not parse times out of replies.
- **Booking approval is always manual** (manager), including for agent-handled meeting requests.
- **Daily cap is per UTC day**, not local day.
- **The suppression list cannot be edited from the UI** (by design). Removing an entry needs a direct database change.
- **The Phase 1 per-provider HTTP rate limiter is in-process.** Running several worker processes multiplies those
  limits. Login, API, MFA, public-chat and customer-send limits are database counters shared by all processes, and
  the job claim is safe across processes. Run one worker.
- **SQLite is the default.** Under concurrent web and worker writes, SQLite serialises them (WAL mode, 30 s timeout).
  Use Postgres for anything beyond one or two users; the suite passes on Postgres 16.
- **Proposals are records only**: there's no document generation or e-signature.
- **Cost tracking** records *estimated* cost per discovery run (editable estimates; free sources default to $0), plus actual costs you enter. Provider invoices
  are not imported. CAC is shown only when won clients and costs exist.
- **No UI for editing a prospect's core fields** (address, phone, website) after creation. New sources fill missing
  fields, and conflicts are kept as evidence. Use merge or add notes; Phase 2 should add an editor with evidence capture.
- **Email open/click tracking is not implemented** (deliberately: no tracking pixels).

## Deployment

- Docker image built and the full stack run in containers (with a sandbox-only CA overlay for `pip`); migrations,
  restart recovery, backup and restore verified. See `docs/audit/deployment-recovery.md`. Not yet built on a normal host.
- `DF_BASE_URL` must be public HTTPS for unsubscribe links and webhooks. No reverse-proxy config is included.
- Backups are a command (`backup`), not a schedule; off-host, encrypted copies are the operator's job.

## Security notes

- **Secrets are not encrypted at rest (production blocker S1).** They can now come from files (`DF_SECRETS_DIR`,
  Docker/Compose secrets) instead of `.env`. The mitigation plan is in `docs/audit/security-findings.md`.
  OAuth refresh tokens are never stored in the database.
- The database and backups are unencrypted; use an encrypted volume and encrypted off-host backups. Expense receipts
  and MFA secrets are encrypted by the app (Fernet, `DF_ENCRYPTION_KEY`); there is no key-rotation command yet.
- TOTP multi-factor authentication with recovery codes is built in (Account security), enforced for agency
  admins/managers and dealership admins in production. There is no self-service MFA reset (an admin clears it in the
  database, see the runbook) and no WebAuthn. Passwords use PBKDF2-SHA256 (390k iterations). Failed logins are
  rate-limited per email+IP in the database (shared by processes, survives restarts).
- The session cookie is `Secure` only when `DF_ENV=production`; run behind HTTPS.
- API keys are per user, shown once and stored hashed. There is no expiry; regenerate to revoke.

## Legal

- **Texas Chapter 302 (telephone solicitation registration, $10,000 bond) is unresolved.** The app positions calls as
  meeting-booking only (the §302.059(1) exemption), but whether a Zoom meeting counts as a "face-to-face" presentation
  hasn't been confirmed. Get advice, or register, before repeated cold calling.
- SMS is deliberately absent (Texas SB 140).

The compliance rules in the code (CAN-SPAM footer and opt-out, suppression, Texas calling hours, no autodialing or
recording) reflect the research in `docs/research/`. They are **not legal advice**. Have counsel review
templates, the data sources you choose, and any move to automated (`auto`) sending before going live.


## Phase 2 (inventory, listings, publishing)

Last updated 2026-10-18 by the Phase 2 chat.

| Item | Status / what's needed |
|---|---|
| Facebook Marketplace publishing | **No API exists** for organic dealer vehicle listings (ended 2021/2023). Publication, edits, removal and buyer messages are manual. The app records staff-attested URLs but cannot verify a Marketplace listing is still live. Browser automation is deliberately not used. |
| Meta vehicle catalog (`meta_catalog`) | Code + mocked-HTTP test only. Needs Meta App Review (`catalog_management` + `business_management`), Business Verification, a vehicle catalog, vehicle detail page URLs and a system-user token. The batch-status response shape is handled defensively but unverified. Items power paid ads, not organic listings. |
| Real inventory feeds | No dealer feed has been connected; feeds tested with mocked HTTP. No SFTP receiver for HomeNet/vAuto-style drops. No DMS vendor presets until real export samples exist. |
| AI listing prose | Off by default; tested with a mocked client only. Template generator cannot apply free-text reviewer feedback (it records it). |
| Spanish | Fixed vocabulary; unknown features/colours stay in English and are flagged for review. |
| Photos | URLs only: no download, reachability check, resizing or moderation. |
| VIN decode | Offline check digit + model-year only; NHTSA vPIC not integrated. |
| Metrics | Listing views, inquiries and sales are not measured (no reliable source), so none are reported. |
| Tenancy | Enforced by tenant-scoped ORM sessions, composite FKs and route checks; PostgreSQL row-level security is not configured. Phase 1 tables (CRM) are agency-wide by design. |
| Dealership users | Created by a dealership admin or agency manager with a temporary password; no invite email, password reset or MFA yet. |
| Docs from Phase 1 | `docs/research/feasibility-report.md` and Phase 1's `docs/research/source-register.md` (IDs like `[S-OSM]`) are referenced but were never committed on the Phase 1 branch. Phase 2 sources are in `docs/research/phase2-source-register.md`. |
| Phase 4 branch | Merged: Phase 4 was ported into `dealerflow` and builds on this Phase 2 code (see Phase 4 below). |

## Phase 4

Every Phase 4 end-to-end test ran with SIMULATED providers and synthetic data. **No real payment, accounting,
PageSpeed, Search Console, Claude API or client email call has been made.**

### Integrations needing credentials or third-party approval

| Integration | Status | What's needed |
|---|---|---|
| Stripe webhooks | Verified locally with self-signed payloads only | Stripe account; a **test-mode** endpoint for `/webhooks/stripe` (events `invoice.paid`, `invoice.payment_failed`, `charge.refunded`) and `DF_STRIPE_WEBHOOK_SECRET`; public HTTPS. Creating Stripe invoices from the app is **not** built (invoices are created here, payments recorded from Stripe events or manually) |
| QuickBooks Online / Xero | **Not implemented** | OAuth app + sandbox company; for now use manual entry, CSV export and bank-CSV reconciliation |
| Google Search Console | **API not implemented**; CSV export import works | Property owner access + OAuth (`webmasters.readonly`) to automate |
| PageSpeed Insights | Parser only, never called | API key (`DF_PAGESPEED_API_KEY`) |
| Claude API | Stubbed-client test only | API key; `DF_AI_ENABLED=true`. Nothing in Phase 4 depends on it |
| Client email (reports, lead summaries) | Mock only | Real provider (Phase 1), `DF_CLIENT_EMAIL_SENDING_ENABLED=true`, and per-client authorized `report_email` connection |
| Listing publication channels (Phase 2) | See the Phase 2 table above | Phase 2 |
| Customer messaging channels (Phase 3) | See the Phase 3 section below | — |

### Functional gaps

* Phase 3 is integrated (migration `0005`); the interim tables survive as `legacy_*` until the removal plan in
  `docs/architecture/phase3-integration.md` is carried out.
* Finance is single-currency for reporting (USD); other currencies are excluded and counted, not converted.
  No sales tax handling. Expense categories are management categories, not tax advice.
* Invoices are not emailed or sent to clients; there is no PDF invoice. Reports are HTML (print to PDF).
* Stripe mapping needs either `invoices.external_id` = Stripe invoice id or `metadata.client_id`; unmatched
  events fail and raise an alert.
* No receipts OCR; no recurring-expense automation (recurrence is a label used by the forecast).
* SEO checks are rule-based; no AI recommendations are generated yet (`detection='ai'` is reserved). JavaScript-
  rendered sites are seen as raw HTML only. External links are not checked (internal only).
* Gross margin covers only directly allocated costs; overhead allocation is not modelled.
* Some automations listed on the client page are gates only: they are not yet wired into the Phase 2
  import/publish code paths (`inventory_sync`, `listing_publish`); `inquiry_auto_response` gates Phase 3 automatic
  replies, and `seo_monitoring` does not schedule audits yet — audits are started by staff; `monthly_report_email` is implemented.
* Client data deletion and export are service functions (no UI) — deliberate, see the runbook.

### Phase 4 security notes

See `docs/security/threat-model.md`. Resolved with the Phase 3 integration: shared (database) rate limits, TOTP
MFA, encrypted receipts. Still open: no external penetration test, `style-src 'unsafe-inline'` kept for Phase 1
templates, no PostgreSQL row-level security, no encryption-key rotation. **The platform is not production-ready**
until live integrations are verified, backups are tested on the production host, and a pilot dealership has been
onboarded with a reviewed customer channel.

## Phase 3 (customer conversations)

Last updated 2026-10-19. Every Phase 3 test ran with SIMULATED delivery and synthetic customers. **No message has
been delivered to a real customer, and no real lead source has been connected.**

| Item | Status / what's needed |
|---|---|
| Website chat delivery | The widget and API work, but staff and automatic replies are recorded as SIMULATED and never shown to the visitor until `DF_CUSTOMER_MESSAGING_LIVE_ENABLED=true` **and** the dealership has a `customer_messaging`/`website_chat` connection marked connected. That live path has only been exercised in automated tests; it has not been reviewed for a real dealership site |
| ADF/XML lead intake | Parser and endpoint tested with synthetic ADF; no real lead provider has posted to it. Replies to ADF leads are drafted only and sent by staff outside the platform |
| Facebook Marketplace messages | No API exists. Staff paste buyer messages into Marketplace intake; replies are copied back by hand |
| Messenger, SMS, email replies to buyers | **Not available.** Phase 3's Meta Messenger and Twilio SMS adapters were code-only and never verified, so they were not ported (they need Meta App Review / A2P 10DLC registration). Buyer replies by email are not built. Notification preferences, the daily digest, the pilot allowlist console and the HMAC-sealed audit log were not ported either |
| Calendar for test drives | No calendar integration: appointment requests are recorded as "pending confirmation" and staff confirm them; the app never claims a slot is free (`availability_checked = "no_calendar"`) |
| AI drafting | Off by default. Drafts are deterministic and grounded in the fact sheet; the optional rephrase uses the shared AI gateway (`DF_AI_ENABLED`) and is re-validated, but has only been tested with a stubbed client |
| Response modes B/C | Require a passing evaluation run (46 synthetic cases, all passing) plus an explicit authorization; tested only on synthetic data |
| Language | English and Spanish only; other languages get a staff handoff |
| Vehicle facts | Price/mileage/availability come from Phase 2 data and are withheld when stale (`stale_inventory_hours`) or conflicting; no live DMS check |
| Global pause | `conversations_global_pause` app setting is enforced but has no UI toggle (use emergency stop or the per-dealership pause) |
| Legacy tables | `legacy_conversations`, `legacy_conversation_messages`, `legacy_appointments` kept after conversion; drop only via the documented removal plan |
| Reporting | Response-time metrics count only delivered replies, so SIMULATED replies do not improve first-response time |
| Notifications | In-app plus e-mail through the gated client-email path (mock by default); no push or SMS alerts to staff |
