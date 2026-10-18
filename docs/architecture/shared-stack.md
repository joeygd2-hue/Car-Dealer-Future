# Shared stack & contracts (all phases MUST follow)

> **Open cross-chat conflict (2026-10-18).** Branch `claude/dealership-inventory-publishing-iq0aqw` contains a separate
> application, `dealerflow/` (Phase 1 + Phase 2), whose own shared-stack doc also claims to be the single foundation.
> Neither app is on `main`. The owner chose (Phase 3.5): keep hardening this `app/` for the dealership pilot and **port
> later**. See "Reconciliation plan" at the end of this file. Do not merge either branch into `main` until that is decided.

Agreed with the project owner on 2026-10-18: **Python FastAPI is the backend for all four phases.** When Phase 3 started, the
repository contained only a README (no Phase 1/2 code on any branch), so Phase 3 created this shared base. Phases 1, 2 and 4
must build on it — do not create a second app, auth system, job runner or navigation.

## Stack
| Concern | Choice | Notes |
|---|---|---|
| Web | FastAPI + Jinja2 server-rendered pages | `app/main.py:create_app()`; routers in `app/web/` |
| DB | SQLAlchemy 2 (typed `Mapped[...]`), SQLite dev / PostgreSQL prod | naive **UTC** datetimes everywhere (`app.db.utcnow()`); convert to the dealership's `timezone` at the edges |
| Migrations | Alembic (`migrations/`) | add models → `alembic revision --autogenerate -m "..."` → review → `alembic upgrade head`; `alembic check` must be clean |
| Background work | `app/services/jobs.py` + `python -m app.worker` | never use threads/cron in request handlers |
| Tests | pytest (`tests/conftest.py` fixtures: fresh SQLite per test, `world` tenant fixture, `run_worker()`, `login()`) | |
| Lint | ruff (`pyproject.toml`) | |
| LLM | optional Anthropic SDK (`uv sync --extra llm`), model from `LLM_MODEL` (default `claude-opus-5-5`) | must be grounded + validated; never source of facts |

## Tenancy & authorization (`app/security/auth.py`)
- Tenant = `Dealership`. Every tenant-owned table has `dealership_id`.
- Roles: `User.is_platform_admin` (operator) · `Membership.role` ∈ `DEALER_ADMIN`, `DEALER_STAFF`.
- Portal routes live under `/portal/{dealership_id}/...` and use `portal_scope` (GET) / `portal_scope_post` (POST, CSRF-checked)
  / `dealer_admin_post`. Fetch rows with `get_owned(db, Model, id, scope.dealership.id)` (404 on mismatch). Admin routes use
  `admin_get` / `admin_post`.
- All state-changing forms include `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`.
- Sessions: httpOnly cookie `cdf_session`, server-side `UserSession` rows (token hashed).

## Model ownership (extend, never duplicate)
| Area | Tables | Owner / notes |
|---|---|---|
| Core | dealerships, business_hours, users, memberships, user_sessions, audit_logs, system_controls, live_authorizations, integration_connections | shared |
| Customer CRM | customer_contacts, customer_identities, customer_leads, contact_suppressions | Phase 3 (dealer's end customers) |
| Inventory | vehicles, listing_drafts | **minimal base created by Phase 3; Phase 2 owns and extends** (ingestion, photos, publishing). Keep `verified_at`, `data_conflict_note`, `approved_features`, `status`, `price_cents` semantics — the response engine depends on them |
| Inbox | conversations, messages, inbound_events, response_drafts, escalations | Phase 3 |
| Automation | automation_settings, mode_authorizations, approved_templates, faqs, evaluation_runs | Phase 3 |
| Appointments | appointments, appointment_events (customer test drives) | Phase 3 |
| Notifications | notification_preferences, notifications | shared — use `services.notifications.notify()` / `notify_operator()` |
| Jobs | jobs, recurring_tasks, worker_heartbeats | shared |
| Outreach / sales CRM | outreach_agent_state, prospects, prospect_facts, campaigns, enrollments, outreach_messages, outreach_replies, suppressions, sales_meetings, cost_entries, outreach_faqs | **Phase 1 foundation** (built here at the owner's request); Phase 1 extends |
| Sandbox | sandbox_records | simulated provider traffic |
| Finance/SEO/analytics | — | Phase 4 adds new tables; reuse `cost_entries` for provider costs |

## Shared service contracts
- **Audit**: `services.audit.record(db, action, actor_type=..., dealership_id=..., entity_type=..., entity_id=..., outcome="OK|FAILED|BLOCKED|SIMULATED", external=bool, **details)` for every external action and state transition.
- **Jobs**: register with `@jobs.handler("area.verb", on_dead=fn)`; enqueue with `jobs.enqueue(db, type, payload, idempotency_key=..., run_at=..., max_attempts=...)`. Handlers run in their own transaction, must be idempotent, raise `RetryLater(delay, reason, persist=fn)` to wait without consuming attempts, `PermanentJobError` to stop. Recurring work: `jobs.ensure_recurring(db, name, job_type, interval_seconds)` from an `install_schedules(db)` hooked into `app/worker.py:install_recurring_tasks`, and import your handler module in `register_all_handlers()`.
- **Kill switches**: `services.controls` — `GLOBAL_AUTOMATION_PAUSE`, `OUTREACH_EMERGENCY_STOP`; `live_authorized(db, scope, dealership_id)`.
- **Integrations**: contracts in `app/integrations/base.py` (`CustomerChannel`, `EmailSender`, `InboxReader`, `CalendarProvider`, `VideoMeetingProvider`, `ProspectSource`, `LLMProvider`); obtain adapters only through `app/integrations/registry.py`, which returns the sandbox adapter unless `LIVE_INTEGRATIONS_ENABLED=true` **and** an unrevoked `LiveAuthorization` for the scope exists **and** credentials are configured. Results report `SENT` vs `SIMULATED` vs `FAILED` — persist and display them truthfully.
- **Credentials**: `security.crypto.encrypt_json/decrypt_json` (Fernet, `ENCRYPTION_KEY`). Never render secrets, never put them in prompts or logs.
- **Calendar/video** (shared by Phase 1 sales meetings and Phase 3 test drives): `registry.calendar(db)` (operator), `registry.dealership_calendar(db, dealership_id)` (per dealer, `None` if not connected), `registry.video(db)`. Customer `Appointment` and operator `SalesMeeting` are separate tables on purpose.
- **Every external send goes through `services.send_gate`** (`open_attempt` → commit → provider call → `close_attempt`).
  It enforces the platform emergency stop (`send_gate.EMERGENCY_STOP_ALL`) and pilot-mode allowlisting, and writes the
  `send_attempts` ledger. New outbound channels MUST use it, claim their row atomically (status UPDATE … WHERE status=…)
  before sending, and treat a row found mid-send as UNCERTAIN (never auto-resend). Adapters must set
  `SendResult.uncertain=True` when the provider may have accepted the request (timeouts after sending).
- **Distributed limits**: `services.rate_limits.try_acquire/acquire_all` (DB counters). Do not use in-process counters
  for anything that must hold across processes (sending limits, budgets, login/chat throttles).
- **Auth extras**: TOTP MFA (`security.mfa`), password reset/setup links (`/password/reset/{token}`, hashed + single
  use), `invalidate_sessions(db, user_id)`. Secrets are only ever shown once in a response body — never in URLs.
- **Customer messaging**: ingest through `services.inbox.processing.ingest_inbound(...)`; send through `services.inbox.outbound.queue_outbound(...)` (stop conditions re-checked at send time). Never call provider adapters directly from routes.

## Live operation rule
Nothing contacts a customer, prospect or provider for real until: env `LIVE_INTEGRATIONS_ENABLED=true` + credentials + a
recorded `LiveAuthorization` (Admin → Live authorizations) for the specific scope. Scopes: `outreach.email`, `outreach.inbox`,
`outreach.discovery.google_places`, `calendar.google`, `video.zoom`, `notifications.email`, `llm.anthropic`,
`customer_messaging.META_MESSENGER` / `customer_messaging.TWILIO_SMS` (per dealership).


## Reconciliation plan with `dealerflow` (to execute only after the owner confirms)
1. Pick one tenancy model. Phase 3 requires per-dealership tenants (client portal, isolation); dealerflow has a single
   business with roles. Recommended: keep dealerflow's operator CRM (prospects/outreach) and add a `clients` table
   (one-to-one with a won prospect) as the tenant root, as its own shared-stack doc suggests.
2. Port Phase 3 modules as services: `responder/*`, `inbox/*`, `automation_policy`, `appointments`, `summaries`,
   `escalation`, `send_gate`, `rate_limits`, `evaluation` + `evals/`. They depend only on SQLAlchemy sessions and the
   integration contracts, so the port is mostly model renames.
3. Keep one job queue (both are DB-backed with atomic claims) and one outreach agent (dealerflow's is the Phase 1 owner;
   carry over this app's send ledger, uncertain-delivery handling and distributed counters).
4. Re-run this branch's tests against the merged app (they encode the pilot guarantees) before retiring `app/`.
