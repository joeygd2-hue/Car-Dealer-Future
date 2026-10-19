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
| `client` | A dealership user, bound to one `clients` row (`users.client_id`) | Only `/portal` and `/api/v1/portal/*`: own published reports, issued invoices, SEO findings, real conversations, own tickets |
| `viewer` | Staff | Read CRM, clients, tickets, SEO, reports |
| `rep` | Staff | Day-to-day actions: clients, onboarding steps, tickets, SEO audits, reports, Phase 2/3 operations |
| `manager` | Staff | Approve outreach, record integration connections and authorizations, toggle automations, read finance |
| `admin` | Staff | Change finance, the service catalog, agreements, users; authorize live operation |

Isolation is enforced in two places: `web/deps.current_user` rejects client users on every path outside the
portal (so no staff route can leak data even if it only uses `current_user`), and portal queries go through
`services/tenancy.scope()` / `owned_or_404()`, which add `client_id = user.client_id` and return 404 for
anything else. Portal routes never accept a client id from the request.

## Phase 4 modules

| Concern | Module | Notes |
|---|---|---|
| Clients and conversion | `services/tenancy.py` | A won prospect becomes a client; its sequences stop and its address is suppressed for prospecting |
| Onboarding, automation gating | `services/onboarding.py` | 14-step checklist; evidence required for authorization/permission/approval steps; service activation blocked until steps 2-11 are complete; each automation lists its prerequisite steps and integrations |
| Tickets | `services/tickets.py` | Manual, portal and system tickets (deduplicated for repeated failures) |
| Service catalog | `services/packages.py` | Three proposed packages seeded at $0; prices are data |
| Finance | `services/finance.py` | Invoices/lines/payments/credits/refunds, agreements, MRR/ARR, metrics, cash flow + forecast, break-even scenario, CSV export, bank reconciliation |
| Usage costs | `services/usage.py` | Extends Phase 1 `cost_entries` with provider/metric/quantity/client and a cost basis; budgets and spike alerts |
| AI gateway | `integrations/llm.py` | Optional; records measured tokens with estimated cost |
| Payments | `integrations/stripe.py` | Signature-verified webhooks, deduplicated by event id, processed by a job |
| SEO | `services/seo_crawler.py`, `seo_checks.py`, `seo_audit.py` | Authorized sites only; read-only (GET/HEAD); issue lifecycle across audits |
| Client reports | `services/client_reports.py` | Immutable monthly snapshots; sources per metric; channel status; attribution disclaimer |
| Client email | `services/client_email.py` | Reports and lead summaries; SIMULATED unless explicitly enabled and authorized per client |
| Retention, export, deletion | `services/maintenance.py` | Purge conversation PII after N days; JSON export; confirmed hard delete |
| Customer conversations (Phase 3) | `models_conversations.py`, `services/conversations/` | See `shared-stack.md` "Phase 3 additions" |

## Data model (Phase 4 additions, migration 0002)

`clients` (1:1 with won `prospects`), `onboarding_steps`, `automation_settings`, `integration_connections`,
`support_tickets`, `attachments`, `webhook_events`, `alerts`, `budgets`, `service_packages`,
`client_agreements`, `invoices`, `invoice_lines`, `payments`, `credit_notes`, `expense_categories`, `expenses`,
`bank_transactions`, `seo_sites`, `seo_audits`, `seo_pages`, `seo_issues`, `search_metrics`, `client_reports`;
new columns `users.client_id`, `cost_entries.{provider, metric, quantity, client_id, verified}`.
The interim Phase 2/3 tables Phase 4 once shipped were replaced by the real Phase 2 tables (`0003`) and the Phase 3
conversation tables (`0005`).

Money is integer cents with a currency (no conversion between currencies; other currencies are excluded and
counted). Timestamps are naive UTC.
