# System design

One application (`dealerflow`, Python/FastAPI) serves the agency's staff and its dealership clients. It runs as
two processes that share one database: the **web** process (`python -m dealerflow web`) and the **worker**
(`python -m dealerflow worker`). Phase-by-phase scope and status: `docs/progress/build-status.md`. Conventions
every phase follows: `docs/architecture/shared-stack.md`.

```
 Staff browser ──┐                                     ┌── Mock providers (default; SIMULATED)
 Client browser ─┼─► web (FastAPI + Jinja, /api/v1) ─┐ │
 Stripe/Zoom/    │    sessions, CSRF, roles,         │ ├── Email: SMTP/IMAP, Gmail API
 Twilio webhooks ┘    client confinement             │ ├── Calendar: Google; video: Zoom
                                                     ▼ ├── Calls: manual / Twilio bridge
                                       PostgreSQL (or SQLite, single host)  ├── Discovery: TxDMV CSV, OSM
                                                     ▲ ├── SEO: client sites (authorized), PageSpeed
 worker: heartbeat → schedule ticks/daily jobs →     │ ├── Payments: Stripe webhooks (test mode first)
         claim jobs atomically → run handlers ───────┘ └── AI (optional): Claude API
```

## Processes and durability

* **Jobs** (`dealerflow/jobs.py`): rows in `jobs` with a unique `key`. `enqueue` is idempotent; a job is
  claimed by one `UPDATE … WHERE status='pending'`; retryable errors back off exponentially; credential and
  permanent errors fail at once; locks older than 15 minutes are released (crash recovery); `on_failure`
  hooks raise alerts/tickets.
* **Scheduler** (`dealerflow/worker.py`): each loop writes a heartbeat, enqueues the agent tick for the
  current time bucket and the daily/hourly jobs (keys contain the bucket, so restarts never duplicate them),
  then runs due jobs. Phase 4 adds `check_budgets` (hourly), `retention_purge` (daily) and
  `monthly_reports` (days 1-3 of each month).
* **Outreach agent** (`services/agent.py`): bounded, checkpointed ticks. See `docs/architecture/autonomous-agent.md`.

## Tenancy and access

| Role | Who | Can do |
|---|---|---|
| Dealership user | Phase 2 model: `users.client_id` + `client_role` (`dealer_admin` / `dealer_reviewer` / `dealer_viewer`) | Only their workspace `/dealers/{id}` (+ `/api/v1/dealers/{id}`, Phase 2 permissions) and the Phase 4 portal `/portal`, `/api/v1/portal/*`: own published reports, issued invoices, SEO findings, real conversations, own tickets |
| `viewer` | Staff | Read CRM, clients, tickets, SEO, reports |
| `rep` | Staff | Day-to-day actions: clients, onboarding steps, tickets, SEO audits, reports, inventory imports and publishing (Phase 2) |
| `manager` | Staff | Approve outreach, record integration connections and authorizations, toggle automations, read finance |
| `admin` | Staff | Change finance, the service catalog, agreements, users; authorize live operation |

Isolation is enforced in layers: `web/deps.current_user` rejects dealership users on every path outside
`DEALER_ALLOWED_PREFIXES` (so no staff route can leak data even if it only uses `current_user`) and
`require()` never admits them; Phase 2 workspace routes go through `tenancy.authorize_client` and a
tenant-scoped session; Phase 4 portal queries go through `services/clients.scope()` / `owned_or_404()`,
which add `client_id = user.client_id` and return 404 for anything else. Portal routes never accept a client
id from the request.

## Phase 4 modules

| Concern | Module | Notes |
|---|---|---|
| Clients and conversion | `services/clients.py` | The one creation path (`/dealers`, `/clients`, API): unique slug, `onboarding` status, checklist; a won prospect's sequences stop and its address is suppressed for prospecting |
| Onboarding, automation gating | `services/onboarding.py` | 14-step checklist; evidence required for authorization/permission/approval steps; service activation blocked until steps 2-11 are complete; each automation lists its prerequisite steps and integrations |
| Tickets | `services/tickets.py` | Manual, portal and system tickets (deduplicated for repeated failures) |
| Service catalog | `services/packages.py` | Three proposed packages seeded at $0; prices are data |
| Finance | `services/finance.py` | Invoices/lines/payments/credits/refunds, agreements, MRR/ARR, metrics, cash flow + forecast, break-even scenario, CSV export, bank reconciliation |
| Usage costs | `services/usage.py` | Extends Phase 1 `cost_entries` with provider/metric/quantity/client and a cost basis; budgets and spike alerts |
| AI gateway | `integrations/llm.py` | Optional; `complete()` records measured tokens with estimated cost (Phase 2's `structured_call` shares the client) |
| Payments | `integrations/stripe.py` | Signature-verified webhooks, deduplicated by event id, processed by a job |
| SEO | `services/seo_crawler.py`, `seo_checks.py`, `seo_audit.py` | Authorized sites only; read-only (GET/HEAD); issue lifecycle across audits |
| Client reports | `services/client_reports.py` | Immutable monthly snapshots; sources per metric; channel status; attribution disclaimer |
| Client email | `services/client_email.py` | Reports and lead summaries; SIMULATED unless explicitly enabled and authorized per client |
| Retention, export, deletion | `services/maintenance.py` | Purge conversation PII after N days; JSON export; confirmed hard delete |
| Inventory, listings, publishing | Phase 2: `models_dealer.py`, `services/inventory`, `services/listings`, `services/publishing` | Reports read `import_runs`, `vehicles`, `listing_revisions`, `listing_reviews`, `channel_publications`, `inventory_sources`, `channel_connections` |
| Interim Phase 3 data | `models_interim.py`, `services/dealer_ops.py` | Conversations and appointments; to be replaced by the Phase 3 port |

## Data model (Phase 4 additions, migration 0003; interim Phase 3 tables in 0004)

Phase 2's `clients` gains `legal_name`, `address_line`, `postal_code`, `phone`, `website`, `contact_name`,
`contact_email`, `languages`, `business_hours`, `response_rules`, `activated_at`, `cancelled_at` and a status
check (`onboarding | active | paused | offboarded`). New tables: `onboarding_steps`, `automation_settings`, `integration_connections`,
`support_tickets`, `attachments`, `webhook_events`, `alerts`, `budgets`, `service_packages`,
`client_agreements`, `invoices`, `invoice_lines`, `payments`, `credit_notes`, `expense_categories`, `expenses`,
`bank_transactions`, `seo_sites`, `seo_audits`, `seo_pages`, `seo_issues`, `search_metrics`, `client_reports`;
new columns `cost_entries.{provider, metric, quantity, client_id, verified}`.
Interim (Phase 3): `conversations`, `conversation_messages`, `appointments`.

Money is integer cents with a currency (no conversion between currencies; other currencies are excluded and
counted). Timestamps are naive UTC.
