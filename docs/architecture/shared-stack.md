# Shared stack and interfaces (all phases)

**Status:** agreed on 2026-10-18 and set up in Phase 1. The repository held only a one-line README
before Phase 1, so no earlier stack existed to keep. Every later phase (2–4) extends this one
application. **Do not create a second app, database, login system or navigation.**

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python ≥ 3.11 (CI/dev on 3.12/3.13) | |
| Web | FastAPI + Starlette sessions | Server-rendered Jinja2 pages under `dealerflow/templates`, JSON API under `/api/v1` |
| ORM / DB | SQLAlchemy 2.x, SQLite in dev/test, Postgres-ready URL via `DF_DATABASE_URL` | Naive UTC timestamps everywhere (`db.utcnow()`) |
| Migrations | Alembic, scripts in `dealerflow/migrations/versions` | `python -m dealerflow migrate`; one migration per change; never edit a released migration |
| Settings | pydantic-settings, env prefix `DF_`, `.env` supported | Secrets come only from env; never stored in DB, logs, templates or prompts |
| Jobs | DB-backed queue in `dealerflow/jobs.py` + worker `python -m dealerflow worker` | Idempotent keys, atomic claim, backoff, `DeferJob`, failure hooks |
| HTTP clients | `dealerflow/integrations/http.py` `request()` | Retries, backoff, token-bucket rate limit, maps 401/403 → `CredentialError` |
| Tests | pytest; `tests/conftest.py` builds a DB with the real migrations | All providers mocked; no network in tests |
| Deploy | Dockerfile + docker-compose (`migrate`, `web`, `worker`) | |

Python package name: `dealerflow`. Run `python -m dealerflow` for the CLI.

## Rules for adding a phase

1. **Models** go in `dealerflow/models.py` (or a new module imported by it so Alembic sees it). Then
   run `python -c "from dealerflow.migrate import revision; revision('phase2 inventory')"`, check the
   generated file, rename it `000N_<slug>.py` with `revision = '000N'`, and commit it.
   `tests/test_migrations.py` fails if models and migrations differ.
2. **Business logic** goes in `dealerflow/services/<area>.py`. **Routes** go in `dealerflow/web/routes_<area>.py`,
   registered in `web/app.py`. Add nav links in `templates/base.html`.
3. **Auth**: use `Depends(current_user)` or `Depends(require("rep"|"manager"|"admin"))` from `web/deps.py`.
   Form posts need the router dependency `check_csrf` and a hidden `csrf` field. Roles, lowest to highest:
   `viewer < rep < manager < admin`.
4. **External providers** go in `dealerflow/integrations/<name>.py` and need: an interface class, a `Mock…`
   implementation with `simulated = True` and `reset()`/`fail_next`, and a real implementation that
   uses `integrations.http.request`. Wrap every call in `audit.track_integration(db, provider, action, simulated=…)`.
5. **Background work**: register handlers with `@jobs.handler("kind")` and import the module in
   `dealerflow/handlers.py`. Enqueue with a deterministic `key`. Handlers must be idempotent.
6. **Audit**: call `audit.audit(db, "area.action", actor_id=…, entity_type=…, entity_id=…)` for every state change
   a person or the agent makes. Add timeline entries with `services.crm.add_activity`.
7. **Simulated data** carries `simulated=True` and is shown with a SIMULATED badge. Reports count it separately.
8. **Never** add an outbound channel (SMS, social DMs, voice) without a compliance gate like
   `services/compliance.py` and an admin authorization switch like `AgentState.live_authorized`.

## Shared entities (owned by Phase 1)

| Entity | Table | Purpose |
|---|---|---|
| `User` | `users` | Login, role, optional API key (hashed) |
| `Prospect` | `prospects` | A dealership. `stage` → `pipeline_stages.key`. `simulated`, `do_not_contact`, `merged_into_id` |
| `Contact` | `contacts` | Verified people at a dealership (`source_url` required) |
| `Evidence` | `evidence` | Every sourced fact: `field`, `value`, `source_type`, `source_url`, `collected_at`, `verified` |
| `ProspectSource` | `prospect_sources` | (source, external_id) links used for dedup |
| `Activity` | `activities` | Prospect timeline (`kind`: research, note, email_out, email_in, call, meeting, proposal, task, stage, agent, system…) |
| `Task` | `tasks` | Follow-ups; `dedupe_key` prevents duplicate automatic tasks |
| `Template`, `OutreachMessage`, `InboundMessage`, `Suppression` | | Email outreach, replies, opt-outs |
| `Meeting`, `Proposal`, `Call`, `CostEntry` | | Sales operations |
| `Campaign`, `Enrollment`, `KnowledgeItem`, `AgentState`, `AgentRun` | | Autonomous agent |
| `AuditLog`, `IntegrationLog`, `Job`, `AppSetting` | | Operations |

A Phase 2+ entity that belongs to a client dealership (inventory, listings, leads from car buyers)
should reference `prospects.id` (a won prospect is a client) rather than adding a separate
"dealer" table. If that becomes awkward, add a `clients` table with a one-to-one `prospect_id`.

## JSON API contract (`/api/v1`)

Authentication: session cookie, or `Authorization: Bearer <key>` (each user generates a key in Settings).
OpenAPI docs at `/api/docs` outside production.

| Method | Path | Role | Body / query | Response |
|---|---|---|---|---|
| GET | `/api/v1/prospects` | viewer | `stage, city, q, limit≤200, offset` | `{total, items:[Prospect]}` |
| POST | `/api/v1/prospects` | rep | `ProspectIn` | `201 {created, matched_by, errors, prospect}` (deduplicated) |
| GET | `/api/v1/prospects/{id}` | viewer | | Prospect + `score_breakdown`, `evidence`, `contacts`, `timeline` |
| POST | `/api/v1/prospects/{id}/stage` | rep | `{stage, reason}` | `409` for workflow-only stages |
| POST | `/api/v1/prospects/{id}/score` | rep | | score breakdown |
| GET | `/api/v1/reports/daily` | viewer | | counts `{real, simulated}` |
| GET | `/api/v1/reports/weekly` | viewer | `include_simulated` | funnel, sources, CAC, recommendations |

Public, unauthenticated endpoints: `GET /healthz`, `GET|POST /u/{token}` (unsubscribe, RFC 8058 one-click),
`POST /webhooks/zoom` (signature-verified), `POST /webhooks/twilio/status` (signature-verified).

## Integration interfaces

| Interface | Module | Methods | Implementations |
|---|---|---|---|
| `EmailProvider` | `integrations/email.py` | `send(OutgoingEmail)→SendResult`, `fetch_inbound(cursor)→(list, cursor)`, `check()` | `mock`, `smtp` (SMTP+IMAP), `gmail` |
| `CalendarProvider` | `integrations/calendar.py` | `busy`, `create_event`, `update_event`, `delete_event`, `check` | `mock`, `google` |
| `ZoomProvider` | `integrations/zoom.py` | `create_meeting`, `update_meeting`, `delete_meeting`, `check` | `mock`, `zoom` (Server-to-Server OAuth) |
| `CallProvider` | `integrations/calls.py` | `start_call` | `manual` (tel: link), `twilio` (agent-first bridge), `mock` |
| Discovery sources | `integrations/discovery.py` | functions returning `LeadRecord` | `fixture` (SIMULATED), `osm` (Google Places client exists but may not be used for stored leads) |

Errors: raise `CredentialError` (not retried), `RateLimitedError` (retried, honours Retry-After),
`PermanentError` (not retried), or `IntegrationError` (retried).


## Phase 4 additions (shared by every phase)

Added on branch `claude/blissful-davinci-82py6w` (migrations `0002`, `0003`). Details:
`docs/architecture/system-design.md`.

### Clients and the `client` role

* `clients` is the tenant boundary for dealership data. A won `Prospect` becomes a `Client`
  (`clients.prospect_id`, unique) via `services.tenancy.create_client()`, which also stops prospecting
  sequences and suppresses the address. Phase 2/3 tables that belong to a dealership must have
  `client_id → clients.id` (indexed, NOT NULL, `ON DELETE CASCADE`).
* Role `client` (lowest rank, `users.client_id` set) may only reach `/portal` and `/api/v1/portal/*`;
  `web.deps.current_user` enforces this for every route. Portal code must use `services.tenancy.scope()` /
  `owned_or_404()` and must never take a client id from the request. Use `Depends(require_client)` for portal
  routes; `require("viewer")` and above never admit client users.
* Staff-side Phase 2/3 routes under `/clients/{cid}/…` must check that the row's `client_id` equals `cid`.

### Shared Phase 4 services other phases should use

| Need | Use |
|---|---|
| Record metered provider usage (AI tokens, API calls) | `services.usage.record_usage(db, provider=, category=, metric=, quantity=, ref=, client_id=)` (idempotent by `ref`) |
| Call Claude | `integrations.llm.complete()` (records tokens; raises `AIUnavailable` when off) |
| Email a client dealership | `services.client_email.send()` (gated; SIMULATED by default) |
| Open a ticket for a client-facing failure | `services.tickets.open_system_ticket(db, client_id=, kind="integration_failure", …, dedupe_key=)` |
| Raise an operator alert | `services.alerts.raise_alert(db, kind, message, severity, dedupe_key)` |
| Gate an automation on onboarding | add an entry to `services.onboarding.AUTOMATION_PREREQS`; check `automation_enabled(db, client_id, feature)` before acting |
| Audit with an entity | `audit.record(db, action, actor=, entity=, details=)` (secret-named keys redacted) |

### Phase 4 interim tables — Phase 2/3 must replace these

`models_interim.py` + migration `0003` define minimal `inventory_imports`, `vehicles`, `listings`,
`publish_attempts`, `conversations`, `conversation_messages`, `appointments`, used only so Phase 4 reporting and
the end-to-end test work before Phases 2/3 land in dealerflow. When the real Phase 2 (inventory, listings,
publishing) or Phase 3 (conversations, appointments) models are ported:

1. Rebase your migration onto `0002` (not `0003`), or add a migration after `0003` that drops the interim tables
   your models replace. Do not keep two definitions of the same table.
2. Delete the replaced classes from `models_interim.py` and the matching functions in `services/dealer_ops.py`.
3. Update `services/client_reports.build_report_data()` to read your tables (keep: one source per metric,
   SIMULATED rows excluded from real counts, channel status from `integration_connections`).
4. Keep `tests/test_e2e_business_journey.py` passing (swap its Phase 2/3 steps to your APIs).
