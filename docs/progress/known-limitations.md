# Known limitations, blockers and unfinished work

_Last updated: 2026-10-18._

## Blocked on credentials / approvals (nothing live has been exercised)
- **Meta Messenger**: needs Meta app, App Review for `pages_messaging` (+ HUMAN_AGENT for replies after 24h), Business Verification, per-Page tokens. Adapter is tested only against mocked HTTP.
- **Twilio SMS**: needs accounts/numbers, A2P 10DLC registration, Spanish keywords in Twilio Advanced Opt-Out, documented consent. Automated SYSTEM messages (confirmations/reminders) over SMS are blocked unless `sms_consent` is recorded.
- **SMTP/IMAP (notifications + outreach)**, **Google Places**, **Google Calendar**, **Zoom**, **Anthropic**: code only; need credentials, `LIVE_INTEGRATIONS_ENABLED=true` and recorded live authorizations.
- **Outreach live sending** additionally requires: CAN-SPAM sender identity + postal address configured, human-verified recipient emails, SPF/DKIM/DMARC on the sending domain. Sandbox drafts show "[… not configured]" placeholders until `OUTREACH_*` is set; live sends with missing identity are blocked.

## Unsupported by providers
- Facebook Marketplace conversations have **no official API**; only the human-assisted workflow exists. Never automate personal accounts.
- Google Business Profile chat was discontinued (2024).
- Dealer CRM APIs are partner-gated; ADF/XML is the interim integration.

## Product/engineering gaps
- Phase 1 and Phase 2 features beyond the minimal foundation are not built (inventory ingestion, listing generation/publishing, Phase 1 research tooling beyond the outreach agent).
- Rule-based NLU: intent/opt-out/language detection is deterministic and conservative; unusual phrasing escalates rather than auto-answers. Time parsing handles weekday/today/tomorrow + clock times only.
- Mode C allows only `test_drive`, `contact_info_provided`, `vehicle_details` as extra intents, and appointment requests always go to staff confirmation (no automatic confirmation, by design).
- Customer-facing appointment confirmations/reminders go only through channels the platform can send on (website chat, Messenger, SMS); Marketplace/ADF customers must be contacted by staff.
- Website chat is polling-based (4s); no websockets. Rate limiting is in-process (use a shared store such as Redis when running multiple web processes).
- At-least-once delivery: if a worker crashes after a provider accepted a message but before commit, a retry could duplicate that one message (providers lack idempotency keys for these endpoints). Documented trade-off.
- Booking page shows UTC times (invite shows local time); no prospect-side reschedule/cancel link yet.
- Daily digest timing depends on the hourly job; digests go to dealership admins.
- SQLite is used in dev/tests; PostgreSQL is supported by SQLAlchemy/Alembic but has not been tested in this environment.
- No password reset / MFA yet; platform-admin creation is via the seed script or direct DB access (add a CLI in Phase 4).
- Retention: raw provider events and finished jobs are purged after 30 days; Places-derived prospect fields are purged after 30 days unless human-verified. Conversation/contact retention policy is not yet configurable per dealership.
- Research items to re-verify before go-live: WhatsApp Oct-2026 pricing, current 10DLC fees, CTIA Spanish keyword guidance, AIA Messenger lead webhooks, Places 30-day field list, Zoom/Graph scope names.
