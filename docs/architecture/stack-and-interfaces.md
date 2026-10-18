# Agreed stack and shared interfaces (all phases)

This is the contract for every Claude chat working on this repository (Phases 1–4).
**One application, one database, one auth system, one job queue.** Extend these; do not create parallel versions.

Status when written (Phase 2 chat, 2026-10-18): the repository contained only a README when Phase 2 began.
No Phase 1 code existed, so Phase 2 created the shared foundation below. A user then asked for a persistent
outreach agent (Phase 1 territory); it is built on the same foundation in `app/outreach/`.
The user specified **Python + FastAPI** for all phases.

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ (developed on 3.13) | |
| Web/API | FastAPI (`app/main.py`), JSON under `/api/*` | OpenAPI docs at `/api/docs` |
| ORM / DB | SQLAlchemy 2.x models in `app/models/*` | SQLite for dev/tests; PostgreSQL for production |
| Migrations | Alembic (`migrations/`) | `alembic revision --autogenerate -m "..."` after model changes |
| Background work | DB-backed durable queue `app/core/jobs.py` + worker `python -m app.worker` | No Redis/Celery needed |
| Frontend | Vanilla JS single-page app in `app/web/static/` served at `/` | No build step. Render untrusted text with `textContent` only |
| AI | Anthropic SDK through `app/core/ai.py` (`structured_call`) | Default model `claude-opus-5-5`; disabled without `ANTHROPIC_API_KEY` |
| Tests | pytest (`tests/`) | Fresh SQLite file per test; no network (httpx `MockTransport`) |
| Config | Environment variables via `app/config.py` (see `.env.example`) | Never commit secrets |

## Package layout

```
app/
  config.py            settings from env
  db.py                engine, sessions, TenantMixin, tenant_session()
  main.py              FastAPI app factory, error handlers, security headers
  worker.py            worker entry point: registers handlers from every phase
  core/                shared services (all phases)
    security.py        password hashing (scrypt), login sessions
    tenancy.py         roles, permissions, authorize_client(), ClientContext
    audit.py           append-only audit log helpers
    crypto.py          AES-GCM secret encryption (APP_ENCRYPTION_KEY)
    jobs.py            durable queue, schedules, system flags
    worker.py          Worker class (bounded ticks, leases, retries, heartbeats)
    ai.py              Claude structured-output wrapper with cost accounting
    errors.py          AppError hierarchy -> JSON errors
  models/              ALL database models (one shared schema)
    core.py            Client, User, ClientMembership, AuthSession, AuditLog, Job, JobSchedule,
                       WorkerHeartbeat, SystemFlag, PlatformIntegration
    inventory.py       Phase 2 inventory
    listings.py        Phase 2 listings + publishing
    outreach.py        CRM prospects + outreach agent (Phase 1 scope)
  inventory/ listings/ publishing/ sync/ reporting/    Phase 2 services
  outreach/            outreach agent (policy, providers, classifier, drafting, agent, jobs)
  api/                 routers: auth, system, inventory, listings (+publishing/reports), outreach
  web/static/          SPA
```

New phases add `app/<domain>/` service packages, `app/models/<domain>.py`, `app/api/<domain>.py`, and register
routers in `app/main.py` and job handlers in `app/worker.py`.

## Shared interfaces

### Tenancy and authorization (mandatory for dealership data)

- A signed dealership is a `clients` row. Dealership-owned tables inherit `TenantMixin` (adds `client_id`).
- Child tables reference parents with **composite** `(client_id, parent_id)` foreign keys and parents declare
  `UniqueConstraint("client_id", "id")`. This makes cross-dealership links impossible at the database layer.
- Routes get a `ClientContext` only through the dependency `client_ctx(permission)` in `app/api/deps.py`, which calls
  `authorize_client()`. `ctx.db` is a **tenant-bound session**: SELECT/UPDATE/DELETE on tenant models get
  `client_id = :tenant` added automatically, and flushing a row for another tenant (or changing `client_id`)
  raises `TenantViolation` (HTTP 403).
- Unknown dealership and "not your dealership" both return 404.
- Roles: `users.platform_role` ∈ {`platform_admin`, `platform_staff`, null}; per-dealership
  `client_memberships.role` ∈ {`dealer_admin`, `dealer_reviewer`, `dealer_viewer`, `agency_operator`}.
  Permissions map: `app/core/tenancy.py::PERMISSIONS`. Agency-level features use `platform_user`/`platform_admin` deps.
- Background jobs use `job_context(client_id, job_name)`.
- **Phase 3 (client portal)** must reuse these roles and `client_ctx`; do not build a separate portal auth.

### Authentication
`POST /api/auth/login` → bearer token (stored hashed in `auth_sessions`); send `Authorization: Bearer <token>`.
`GET /api/me` returns the user and the dealerships they can access.

### Audit log
`app/core/audit.py::audit()` / `audit_ctx()` → `audit_log` (append-only; updates/deletes are refused).
Record every state transition, external call, error and cost (`cost_usd`). Actor types: user, system, job, agent.

### Durable jobs
```python
from app.core.jobs import enqueue, upsert_schedule
enqueue(db, "phase3.route_lead", client_id=..., payload={...}, idempotency_key="lead:123")
upsert_schedule(db, "phase3.digest", "phase3.digest", interval_seconds=3600)
```
Handlers have the signature `handler(db, job, payload) -> dict` and are registered in `app/worker.py`.
Raise `NonRetryableError` for permanent failures; any other exception retries with exponential backoff up to
`max_attempts`, then the job is `dead` (visible in `/api/system/status`). Use idempotency keys for anything that
must not run twice. Keep handlers bounded (the worker enforces a time budget and a lease).

### Secrets and provider credentials
Encrypt with `app/core/crypto.py::encrypt_secret`. Dealer-specific credentials: `channel_connections` (tenant-scoped).
Agency credentials: `platform_integrations`. APIs return `has_secret: true`, never the secret.

### Live-action gating (do not weaken)
Anything that reaches a real person or platform requires **both** an environment switch and a recorded human
authorisation: Meta (`META_LIVE_PUBLISHING_ALLOWED` + connection `live_publishing`), outreach
(`OUTREACH_LIVE_ALLOWED` + integration `live_authorized` + campaign `live_authorized`). Global outreach pause and
emergency stop live in `system_flags` (`outreach.global_pause`, `outreach.emergency_stop`).

### AI usage
Use `app/core/ai.structured_call(system, user, PydanticSchema)`. Pass only verified data. Treat model output as
untrusted: validate it (see `app/listings/guardrails.py`, `app/outreach/policy.py`). Record `AIResult.cost_usd`
as a measured cost.

### Cross-phase data hooks
| Need | Use |
|---|---|
| Phase 1 CRM lead → client | `prospects.converted_client_id` and `clients.source_prospect_id` |
| Phase 3 customer messages about a vehicle | `vehicles.id`, `listings.id`, `channel_publications` (Marketplace messages are not API-accessible; see known limitations) |
| Phase 3 approved dealership facts | `dealer_listing_settings.settings` (contact, hours, disclaimers, financing statement) |
| Phase 4 analytics | `audit_log`, `vehicle_changes`, `publish_operations`, `outreach_messages`, `cost_ledger` |
| Phase 4 cost tracking | `cost_ledger` (`measured` = provider-reported vs estimate) |

## Conventions
- IDs: prefixed strings (`cli_`, `veh_`, `lst_` …) from `app/core/time.new_id`. Timestamps are UTC (`UTCDateTime`).
- API errors: `{"error": {"code", "message", "details"}}`.
- Never fabricate data: missing facts stay `null` and are surfaced as validation issues or uncertainty notes.
- Every new external integration gets a row in `docs/architecture/integration-matrix.md` with a verified status.
