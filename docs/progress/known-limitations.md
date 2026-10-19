# Known limitations (Phases 1, 1.5, 2 and 4, integrated)

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
- **The rate limiter is in-process.** Running several worker processes multiplies the per-provider HTTP limits.
  The job claim is still safe across processes. Run one worker.
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
- Data at rest (database, backups) is unencrypted; use an encrypted volume and encrypted off-host backups.
- No multi-factor authentication. Passwords use PBKDF2-SHA256 (390k iterations). Failed logins are rate-limited per email+IP
  (in-memory, so it resets on restart).
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
| Phase 4 branch | `claude/blissful-davinci-82py6w` is still a separate app (`app/` package) and is not merged; it must be ported into `dealerflow`. |
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
| Listing publication channels (Phase 2) | None; all attempts `manual_required` | Phase 2 |
| Customer messaging channels (Phase 3) | Website chat, ADF leads and staff-assisted Marketplace work in the app; every reply is SIMULATED by default | See "Phase 3 integration" below |

### Functional gaps

* Finance is single-currency for reporting (USD); other currencies are excluded and counted, not converted.
  No sales tax handling. Expense categories are management categories, not tax advice.
* Invoices are not emailed or sent to clients; there is no PDF invoice. Reports are HTML (print to PDF).
* Stripe mapping needs either `invoices.external_id` = Stripe invoice id or `metadata.client_id`; unmatched
  events fail and raise an alert.
* No receipts OCR; no recurring-expense automation (recurrence is a label used by the forecast).
* SEO checks are rule-based; no AI recommendations are generated yet (`detection='ai'` is reserved). JavaScript-
  rendered sites are seen as raw HTML only. External links are not checked (internal only).
* Gross margin covers only directly allocated costs; overhead allocation is not modelled.
* Automations listed on the client page are gates: `inquiry_auto_response` is now checked by the Phase 3 reply
  policy (Modes B/C also need it); the `inventory_sync` and `listing_publish` gates are not checked by Phase 2's
  workers yet; `seo_monitoring` does not schedule
  audits yet — audits are started by staff; `monthly_report_email` is implemented.
* Client data deletion and export are service functions (no UI) — deliberate, see the runbook.

### Phase 4 security notes

See `docs/security/threat-model.md`. Open items: in-process rate limiting (single process only), no MFA,
attachments unencrypted at rest on local disk, no external penetration test, `style-src 'unsafe-inline'` kept
for Phase 1 templates. **The platform is not production-ready** until live integrations are verified, MFA or
an identity-aware proxy is in place and backups are tested.

## Integration (2026-10-19)

* After login, a dealership user lands on `/portal` (Phase 4 behaviour); the workspace (inbox, test drives,
  customer replies) is one click away and `/` redirects there. One landing page still needs choosing.
* Downgrading migration `0003` maps every `cancelled` client back to Phase 2's `offboarded` and drops the Phase 4
  client columns and tables (their data is lost). Back up first (`python -m dealerflow backup`).
* Deleting a client (service function only, no UI) anonymizes and deactivates its users instead of deleting them.
* `clients.prospect_id` keeps Phase 2's plain foreign key (no `ON DELETE SET NULL`); prospects linked to a client
  should be merged, not deleted.
* Vehicle photos are stored as URLs from the dealer's feed; there is no photo upload or image hosting yet. The CSP
  allows `https:` images so those URLs render; non-HTTPS photo URLs will not display.
* VIN check digits and model years are validated locally, but VINs are **not decoded** (no NHTSA vPIC call is
  implemented, although it was researched in `docs/research/meta-and-inventory-integrations-2026-10.md`).


## Phase 3 integration (2026-10-19)

Customer conversations run inside dealerflow (`claude/gifted-phase3-integration`). Nothing has been sent to a real
customer; every test used synthetic data and SIMULATED channels.

* **Messenger and SMS live sending are not available.** Phase 3's Meta Messenger and Twilio adapters, their
  webhooks (`/webhooks/meta`, `/webhooks/twilio/sms`) and per-dealership encrypted credentials were not ported. A
  conversation on those channels uses the sandbox adapter (SIMULATED) even with every live flag on.
* **Website chat has never been live.** Live means the reply appears in the dealership's own chat widget; it needs
  `DF_CUSTOMER_MESSAGING_LIVE_ENABLED=true`, a `connected` website-chat connection and, in pilot mode (default), an
  allowlisted visitor id. Tested only with the test client.
* **Staff notification email is always SIMULATED** (recorded in `sandbox_records`). It does not use the Phase 1 email
  provider yet. In-app notifications work. Lead summaries to the dealership use Phase 4's client email gate
  (SIMULATED unless `DF_CLIENT_EMAIL_SENDING_ENABLED` and the client's `report_email` authorization).
* **Test-drive calendar is a sandbox.** A `calendar` connection enables the free/busy check and a SIMULATED event;
  Phase 1's Google Calendar provider is not wired to test drives. Staff confirm every appointment; the customer is
  never told a time is booked until staff confirm.
* **Not ported from Phase 3's security work:** TOTP MFA, server-side sessions with revocation and the HMAC-chained
  audit log. dealerflow keeps its own signed-cookie sessions and plain audit log. Phase 3's "return to sandbox"
  button and live-authorization records were replaced by Phase 4 integration connections (status + written note).
* No operator screen to reconcile an UNCERTAIN staff email (customer messages can be reconciled from the
  conversation page).
* The evaluation set (46 cases) is synthetic, and Modes B/C have only been exercised with it and with tests.
* Downgrading migration `0004` restores the interim tables but loses data that only exists in Phase 3 form
  (identities, drafts, escalations, FAQs, templates, settings). Back up first.
* The public chat endpoint's per-IP limit uses the connecting address. Behind a reverse proxy, unless uvicorn runs with
  `--proxy-headers` and trusted `--forwarded-allow-ips`,
  every visitor shares the proxy's address.
* Two other Phase 3 integration attempts exist on the remote (`claude/phase3-integration`,
  `claude/mybot-phase3-integration`), built on a different base (`claude/mybot-unification`). They were not compared
  file by file; pick one line before merging.
