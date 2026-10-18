# Shared stack and contracts (all phases)

**Status:** agreed stack for all four phases. This document was established in the Phase 4 conversation because the repository contained no code from Phases 1–3 when Phase 4 started (only a one-line README, one commit, no other branches or PRs). Other phases should **extend** what is described here, not create parallel apps, models, auth, or queues.

## Stack

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.12+ (developed on 3.13) |
| Web framework | FastAPI (JSON API under `/api/v1`) + server-rendered Jinja2 pages (`/app` staff, `/portal` clients) |
| ORM / DB | SQLAlchemy 2.x; PostgreSQL in production, SQLite for local dev/tests |
| Migrations | Alembic (`migrations/`). Every schema change is a new revision; never edit `0001`. |
| Background work | DB-backed durable job queue + scheduler (`app/services/jobs.py`), run by `python -m app.worker` |
| HTTP client | `httpx` (inject a client/transport for tests; no live network in tests) |
| AI | Optional. `app/integrations/llm.py` (official `anthropic` SDK) records token usage; deterministic templates/rules when disabled |
| Tests | `pytest`, synthetic data, sandbox providers only |

## Directory layout

```
app/
  config.py            settings from env (.env.example lists all)
  db.py                engine/session, Base with naming convention
  models/              core.py (identity, tenancy, jobs, integrations, tickets, onboarding)
                       outreach.py (Phase 1: prospects, agent, campaigns, messages, replies, meetings)
                       dealer_ops.py (Phase 2/3: vehicles, listings, publishing, conversations, appointments)
                       finance.py, costs.py, seo.py (Phase 4)
  security/            auth.py (sessions, API tokens, CSRF, roles, tenant scoping), crypto.py, sensitive.py, rate_limit.py
  services/            business logic (no HTTP). Web routes and jobs call these.
    agent/             autonomous outreach agent (state, compliance gate, discovery, enrichment, classify, messaging, runner, dashboard)
    seo/               crawler, checks, audit
  integrations/        provider adapters: email, inbox, calendar, llm, stripe_webhooks
  web/                 api_*.py (JSON), pages.py (staff UI), portal.py (client UI), public.py (unsubscribe, webhooks)
  templates/, static/
  worker.py, cli.py
migrations/            Alembic
tests/
```

## Conventions every phase must follow

1. **Tenancy.** A dealership client (`clients` row) is the tenant boundary. Every tenant-owned table has `client_id NOT NULL` (indexed, FK `ON DELETE CASCADE`). Queries on behalf of a client user go through `security.auth.scope_to_user()` / `get_owned_or_404()`. Portal endpoints never accept a `client_id` from the request.
2. **Roles.** `owner`, `admin`, `staff` (internal) and `client` (dealership user bound to one client; enforced by a DB check constraint). Use `require_staff`, `require_admin`, `require_client_user` dependencies.
3. **Auth.** Browser: server-side session cookie (`dgs_session`, HttpOnly, SameSite=Lax) + CSRF token on every state-changing request (form field `csrf_token` or `X-CSRF-Token`). Server-to-server: `Authorization: Bearer dgs_...` API tokens (only hashes stored).
4. **Money.** Integer minor units (`*_cents`) plus an ISO currency. Fractional provider costs use micro-units (`*_micros`). Never floats.
5. **Time.** Naive UTC `DateTime` (`models.base.utcnow()`); calendar `Date` for business dates.
6. **Side effects are idempotent.** Use idempotency keys (`jobs.idempotency_key`, `outbound_emails.idempotency_key`, `outreach_messages.idempotency_key`, `usage_records.idempotency_key`, unique provider message/event ids).
7. **Background work** goes through `services.jobs.enqueue(kind, payload, idempotency_key=...)` with a handler registered via `@jobs.handler("kind")`; recurring work via `jobs.ensure_scheduled_task()`. Do not add another queue or cron system.
8. **Outbound communication** goes through `integrations.email.send_email(..., allow_live=...)`. Default provider is `sandbox` (stored, never sent). Prospect outreach must go through `services.agent.messaging.send_message()`, which runs the compliance gate.
9. **Integrations status** is recorded in `integration_connections` (`not_connected | sandbox | connected | error | disabled`). `connected` only after a verified call or a documented authorization.
10. **Audit.** Security- and business-relevant actions call `services.audit.record()`. Agent actions call `services.agent.state.log_activity()`.
11. **Usage/cost.** Any metered provider call records usage via `services.costs.record_usage()` with cost basis `verified_billing | estimated | unknown`.
12. **Secrets** only from env or `integration_credentials` (Fernet-encrypted). Never log them; the root logger has a redaction filter.
13. **Approval-first.** AI- or template-generated customer/prospect-facing content is drafted for human approval unless an admin has explicitly authorized auto-send for that scope.

## Shared interfaces (for Phases 1–3 to extend)

| Interface | Location | Notes |
|---|---|---|
| Inventory import | `services/dealer_ops.import_inventory_csv()` | Phase 2: add feed/API importers that produce `InventoryImport` + `Vehicle` rows |
| Listing generation | `services/dealer_ops.generate_listings()` | Deterministic EN/ES template today; Phase 2 may add AI via `integrations/llm.complete()` (records usage) — still `draft` until approved |
| Publishing | `services/dealer_ops.publish_listing()` | Records `PublishAttempt` (`published_confirmed` only on API confirmation; `manual_required` otherwise). `SUPPORTED_PUBLISH_CHANNELS` is empty. |
| Customer inquiries | `services/dealer_ops.receive_inquiry()`, `draft_response()`, `approve_and_send_response()`, `summarize_and_deliver()` | Phase 3: connect real channels; keep timestamps (`first_inbound_at`, `first_response_at`) for reporting |
| Prospect outreach | `services/agent/*` | Persistent agent; see `docs/architecture/system-design.md` |
| Reporting inputs | `services/reports.build_report_data()` | Reads the tables above; add new metrics only with a recorded source |

## API surface (summary)

- Staff: `/api/v1/clients`, `/clients/{id}/onboarding`, `/clients/{id}/automation/{feature}`, `/clients/{id}/integrations`, `/tickets`, `/clients/{id}/inventory/import`, `/clients/{id}/listings/*`, `/clients/{id}/inquiries/sandbox`, `/clients/{id}/conversations/*`, `/audit-log`
- Admin (finance): `/packages`, `/agreements`, `/invoices`, `/payments`, `/credit-notes`, `/expense-categories`, `/expenses`, `/finance/report`, `/finance/cash-flow`, `/finance/break-even`, `/finance/export/{kind}.csv`, `/finance/bank-import`, `/finance/reconciliation`, `/costs/*`, `/alerts`
- Agent: `/api/v1/agent/{dashboard,state,emergency-stop,live-mode,config,tick,templates,campaigns,facts,prospects,messages,inbound,sandbox/inbound,suppressions,activity}`
- SEO/reports: `/api/v1/seo/{sites,audits,issues,search-metrics/import}`, `/api/v1/reports`
- Portal: `/api/v1/portal/{me,reports,invoices,seo-issues,conversations,tickets}`
- Public: `/unsubscribe/{token}`, `/api/v1/webhooks/stripe`, `/api/v1/webhooks/inbound-email`, `/healthz`, `/readyz`

Interactive OpenAPI docs are served at `/api/docs` outside production.
