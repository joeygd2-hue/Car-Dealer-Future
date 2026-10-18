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
| Discovery sources | `integrations/discovery.py` | functions returning `LeadRecord` | `fixture` (SIMULATED), `osm`, `google_places` |

Errors: raise `CredentialError` (not retried), `RateLimitedError` (retried, honours Retry-After),
`PermanentError` (not retried), or `IntegrationError` (retried).

## Phase 2 additions (dealership inventory, listings, publishing)

Added by the Phase 2 chat on top of this foundation (no second app, login or queue).

**Clients and tenancy.** A signed dealership is a `clients` row (`clients.prospect_id` links the won CRM prospect;
create it from `/dealers`). Dealership-owned tables inherit `models_dealer.TenantMixin` (`client_id NOT NULL`) and
reference parents through composite `(client_id, parent_id)` foreign keys, so the database refuses cross-dealership
links. `tenancy.tenant_session(client_id)` filters every query on tenant models and refuses writes for another client.
Web routes get a `ClientContext` only through `routes_dealers.dealer_ctx(permission)` → `tenancy.authorize_client`.
Another client's ids return 404. **Phase 3 (client portal) must reuse `clients`, `dealer_ctx` and these rules.**

**Dealership users.** `users.client_id` + `users.client_role` (`dealer_admin | dealer_reviewer | dealer_viewer`).
Such users can only open `/dealers/{their id}/...` and `/api/v1/dealers/{their id}/...`; `web/deps.current_user`
returns 403 for every other path and `require()` rejects them, so they never see the CRM, agent or settings.
Staff open any client workspace; viewer = read, rep = import/edit/publish, manager/admin = also settings and channel
connections. Staff approve listings only if the dealership allows agency approval in its settings.

| Area | Module |
|---|---|
| Models + migration | `models_dealer.py`, `migrations/versions/0002_phase2_inventory_listings.py` |
| Tenancy | `tenancy.py` |
| Inventory | `services/inventory/` (parsers, normalize, validate, vin, importer, feeds, reactions) |
| Listings | `services/listings/` (dealer_settings, facts, i18n, template_generator, ai_generator, guardrails, service) |
| Publishing | `services/publishing/service.py`, channels in `integrations/listing_channels.py` |
| AI | `integrations/llm.py` (`structured_call`; optional, off by default; Phase 3/4 should reuse it) |
| Jobs | `services/dealer_jobs.py`: `inventory_sync`, `listing_generate`, `publish_operation`, `inventory_stale_scan`; scheduled from `worker.schedule_tick` |
| Reports | `services/dealer_reports.py` |
| Pages / API | `web/routes_dealers.py` (`/dealers/...`), `web/routes_dealer_api.py` (`/api/v1/dealers/{client_id}/...`) |

`ListingChannel` interface (`integrations/listing_channels.py`): `build_payload`, `validate_payload`, `publish`,
`update`, `delete`, `check_status` → `ChannelResult(outcome=confirmed|submitted|manual_required|failed)`; each channel
documents itself in `DOCS`. Implementations: `facebook_marketplace` (manual), `meta_catalog` (API, guarded), `mock`
(SIMULATED). Same error conventions as the other providers. A listing is `published_confirmed` only after a channel
confirms (`confirmation_type=api`) or a person records the live URL with an attestation (`manual_attested`).

Phase 2 JSON API (`/api/v1/dealers/{client_id}`, same auth): `GET vehicles`, `GET vehicles/{id}`, `POST imports`
(multipart), `GET imports/{id}`, `GET listings`, `GET listings/{id}`, `POST listings/{id}/approve`,
`POST listings-batch/preview|approve`, `GET publications`, `GET publications/{id}`, `POST publications/{id}/publish`,
`POST publications/{id}/confirm-manual`, `GET reports/summary`, `GET export.csv`.

Secrets stay in the environment: a feed credential is read from the variable named in `inventory_sources.secret_env`
(must start `DF_FEED_`), a Meta token from `channel_connections.token_env` (must start `DF_META_TOKEN_`).
