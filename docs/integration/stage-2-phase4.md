# Stage 2 — merge Phase 4 into the integrated Phase 1 + 2 tree

_Written from a trial merge (`git merge --no-commit`) before committing. Merging Phase 4 head `194d247`
(local tag `archive/phase4-194d247`) into stage 1 (`2c3333a`)._

## What changed since the decision document
The Phase 4 chat kept working after `docs/architecture/foundation-decision.md` was written: its branch now
already merges the latest Phase 1 (`427b137`) and Phase 2 (`f174a06`), adopts Phase 2's `clients` /
`vehicles` / `listings`, renumbers its migrations after Phase 2's `0002`, and drops its interim
inventory/listing/publishing tables. That is the resolution the decision document asked for, so this stage
merges that work instead of redoing it, and reviews it.

## Affected scope (the delta this stage adds on top of stage 1)

| Area | Change |
|---|---|
| **Tables (new, 26)** | Phase 4 core (`0003`): `integration_connections`, `support_tickets`, `onboarding_steps`, `automation_settings` (per-client feature gates), `attachments`, `webhook_events`, `alerts`, `budgets`, `service_packages`, `client_agreements`, `invoices`, `invoice_lines`, `payments`, `credit_notes`, `expense_categories`, `expenses`, `bank_transactions`, `seo_sites`, `seo_audits`, `seo_pages`, `seo_issues`, `search_metrics`, `client_reports`. Interim Phase 3 stand-ins (`0004`): `conversations`, `conversation_messages`, `appointments`. |
| **Tables (altered)** | `clients` (Phase 2's table is kept): +12 nullable/defaulted onboarding and contact columns, status check constraint `onboarding/active/paused/offboarded` (a superset of Phase 2's values). `cost_entries`: +`provider`, `metric`, `quantity`, `client_id` (FK SET NULL), `verified`. |
| **Migrations** | `0001` (P1) → `0002` (P2, unchanged byte-for-byte) → `0003_phase4_core` → `0004_interim_phase3_tables`. Linear, single head, additive only. |
| **APIs / routes** | Staff: `/clients`, `/tickets`, `/finance/*`, `/packages`, `/costs`, `/alerts`, `/seo/*`, `/client-reports/*`; JSON `/api/v1/...` equivalents (`routes_ops_api.py`). Client portal: `/portal`, `/api/v1/portal/*`. Public: `POST /webhooks/stripe` (signature-verified, deduplicated by event id). |
| **Jobs** | `check_budgets` (hourly), `retention_purge` (daily: blanks contact details and bodies of interim conversations older than `DF_RETENTION_DAYS_CONVERSATIONS`), `monthly_reports`, `stripe_event`, `seo_audit`. Scheduled from the existing worker daily scheduler. |
| **Auth rules** | Dealership users (`client_id` set) may also use `/portal` and `/api/v1/portal`; `require_client` dependency for portal routes; staff user admin refuses to edit a client user or assign a non-staff role; client users land in their own workspace after login. Session revocation and CSRF unchanged. |
| **Dependencies** | `beautifulsoup4`, `email-validator`; optional `anthropic>=1.15`. |

## Conflicts (trial merge)
`README.md`, `docs/progress/build-status.md`, `dealerflow/web/deps.py`. Phase 4's `deps.py` is stage 1's
resolution plus the portal prefixes and `require_client` (checked: keeps password-version revocation and
dealer confinement for both cookie and API-key auth), so it is taken. The two docs are taken from Phase 4
and rewritten at the end of the integration.

## Data-loss risk
None from the merge: only new tables and new nullable/defaulted columns. The `clients` status check
constraint would reject an upgrade only if a row held a status outside the four values — Phase 2 never
writes one. `retention_purge` deliberately redacts old interim conversations on a schedule; it is existing,
configurable Phase 4 behaviour, not something the merge introduces.

## Security review of the delta
- Kept: tenant isolation (Phase 2 composite FKs + tenant sessions; Phase 4 portal confined to the user's
  client), CSRF, session revocation, audit (`audit.record` adds key-based redaction of secrets).
- **Gap found (pre-existing on the Phase 4 branch): the SEO crawler fetches staff-entered dealer websites
  with no SSRF guard and with automatic redirects.** Fixed in a separate commit in this stage (every hop
  checked against non-public addresses; redirects followed manually).
- Known, not fixed here: in-memory API rate limiter (`web/app.py`) and login limiter are per-process —
  replaced by DB counters in stage 4.

## Gate
Full suite on SQLite and Postgres; migrations match models.
