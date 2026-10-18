# Known limitations, blockers and unfinished work

_Last updated: 2026-10-18 (Phase 3.5)._

## Architecture / coordination
- **Two competing application foundations.** This branch (`app/`, multi-tenant, Phase 3) and
  `claude/dealership-inventory-publishing-iq0aqw` (`dealerflow/`, single-business, Phase 1+2). Neither is on `main`.
  Owner decision (Phase 3.5): harden here, port later. Reconciliation plan: `docs/architecture/shared-stack.md`.

## Blocked on credentials / approvals (nothing live has been exercised)
- **No provider has been tested with a real account.** All provider tests use mocked HTTP or sandbox adapters.
- **Meta Messenger**: Meta app, App Review (`pages_messaging` etc. + HUMAN_AGENT for >24h replies), Business Verification, per-Page tokens.
- **Twilio SMS**: accounts/numbers, A2P 10DLC brand + campaign, Spanish keywords in Advanced Opt-Out, documented consent. Automated SYSTEM SMS needs recorded `sms_consent`.
- **SMTP/IMAP, Google Places, Google Calendar, Zoom, Anthropic**: credentials + `LIVE_INTEGRATIONS_ENABLED=true` + a recorded live authorization per scope; in pilot mode also an allowlisted recipient.
- **Outreach live sending** additionally needs CAN-SPAM identity + postal address, human-verified recipient emails, SPF/DKIM/DMARC, and legal review.

## Unsupported by providers
- Facebook Marketplace conversations: **no official API** — human-assisted workflow only. Google Business Profile chat: discontinued. Dealer CRM APIs: partner-gated (ADF/XML is the interim path).

## Security
- **MFA**: TOTP only (no WebAuthn/passkeys yet). Required for platform admins only when `APP_ENV=production` (or `REQUIRE_ADMIN_MFA=true`); optional for dealer users. No admin-side MFA reset flow — recover with recovery codes or a DB operation.
- **Password reset email** goes through the notification email path; in sandbox it is SIMULATED and the link is visible to platform admins in the sandbox records. Reset links expire in 60 min (setup links 72 h) and are single-use.
- **Audit log**: per-row HMAC + id-gap detection + ORM append-only. A database superuser can delete the *newest* rows without detection and can disable checks entirely; ship audit rows to append-only external storage for production. Rotating `ENCRYPTION_KEY` invalidates existing seals (and stored credentials) — there is no key-rotation tool yet.
- **No account lockout notifications**; throttling is per IP and per account (DB counters).
- **Data at rest**: provider credentials and MFA secrets are encrypted; other data (messages, contacts) relies on database/disk encryption provided by the host.
- No per-dealership data-retention configuration; raw provider events and finished jobs are purged after 30 days.

## Sending / worker
- **At-most-once with human reconciliation**: when a provider may have accepted a message (crash or timeout mid-send), the message becomes UNCERTAIN and waits for an operator decision in the Pilot console. This avoids duplicates but means such messages are not delivered automatically.
- HTTP 5xx / 429 responses are treated as "not accepted" and retried; if a provider ever processes a request but returns 5xx, a duplicate is possible (documented provider behaviour suggests this is rare).
- SQLite concurrency: correct but serialized (one writer). Use PostgreSQL for multiple workers in production (untested here).
- Fixed-window counters allow up to 2× a limit across a window boundary (e.g. 8 at 09:59 + 8 at 10:00).

## Product gaps
- Phase 1/2 features beyond the minimal foundation live in the other branch (`dealerflow`).
- Rule-based NLU (intent/opt-out/language/time parsing): conservative; unusual phrasing escalates. Time parsing: weekdays/today/tomorrow + clock times.
- Mode C extra intents limited to `test_drive`, `contact_info_provided`, `vehicle_details`; appointments always need staff confirmation.
- Website chat is polling-based; booking page shows UTC times; no prospect-side reschedule link.
- Research items to re-verify before go-live: WhatsApp pricing, current 10DLC fees, CTIA Spanish keyword guidance, AIA Messenger lead webhooks, Places field storage rules, Zoom/Graph scope names.
