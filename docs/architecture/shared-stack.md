# Shared stack and interfaces (all phases)

**Status:** agreed on 2026-10-18 and set up in Phase 1. The repository held only a one-line README
before Phase 1, so no earlier stack existed to keep. Every later phase (2–4) extends this one
application. **Do not create a second app, database, login system or navigation.**

> **Branch status (2026-10-19):** Phases 1, 1.5, 2, 3 and 4 run in this one `dealerflow` app (integration branch
> `claude/phase3-integration`, cut from `claude/phase4-integration` plus the fixes from `claude/mybot-unification`).
> Phase 3's separate `app/` package (`claude/magical-darwin-nncp64`) was ported onto the shared models; the interim
> conversation tables are gone (migration `0005`). See "Phase 3 additions" below.

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
8. Every provider call goes through `audit.track_integration`: that is where the emergency stop and the
   disabled-provider gate are enforced. Don't call a provider outside it.
9. **Never** add an outbound channel (SMS, social DMs, voice) without a compliance gate like
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

## Phase 2 additions (dealership inventory, listings, publishing)

Added by the Phase 2 chat on top of this foundation (no second app, login or queue).

**Clients and tenancy.** A signed dealership is a `clients` row (`models_ops.Client`, shared with Phase 4;
`clients.prospect_id` links the won CRM prospect; create it from `/clients`, which calls
`services.tenancy.create_client`). Dealership-owned tables inherit `models_dealer.TenantMixin` (`client_id NOT NULL`) and
reference parents through composite `(client_id, parent_id)` foreign keys, so the database refuses cross-dealership
links. `tenancy.tenant_session(client_id)` filters every query on tenant models and refuses writes for another client.
Web routes get a `ClientContext` only through `routes_dealers.dealer_ctx(permission)` → `tenancy.authorize_client`.
Another client's ids return 404. **Phase 3 (client portal) must reuse `clients`, `dealer_ctx` and these rules.**

**Dealership users.** `users.role = "client"` + `users.client_id` + `users.client_role` (`dealer_admin |
dealer_reviewer | dealer_viewer`), created only through `services.tenancy.create_client_user`. Such users can only open
their portal (`/portal`, `/api/v1/portal/*`) and `/dealers/{their id}/...`, `/api/v1/dealers/{their id}/...`;
`web/deps.current_user` returns 403 for every other path (`CLIENT_ALLOWED_PREFIXES`) and `require()` rejects them, so
they never see the CRM, agent, finance or settings. `GET /` sends them to `/portal`.
Staff open any client workspace; viewer = read, rep = import/edit/publish, manager/admin = also settings and channel
connections. Staff approve listings only if the dealership allows agency approval in its settings.

| Area | Module |
|---|---|
| Models + migration | `models_dealer.py`, `migrations/versions/0003_phase2_inventory_listings_publishing.py` |
| Tenancy | `tenancy.py` |
| Inventory | `services/inventory/` (parsers, normalize, validate, vin, importer, feeds, reactions) |
| Listings | `services/listings/` (dealer_settings, facts, i18n, template_generator, ai_generator, guardrails, service) |
| Publishing | `services/publishing/service.py`, channels in `integrations/listing_channels.py` |
| AI | `integrations/llm.py` (`structured_call`; one gateway with Phase 4's `complete`; off unless `DF_AI_ENABLED`) |
| Jobs | `services/dealer_jobs.py`: `inventory_sync`, `listing_generate`, `publish_operation`, `inventory_stale_scan`; scheduled from `worker.schedule_tick`. Scheduled syncs need the client's `inventory_sync` automation |
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

## Phase 4 additions (shared by every phase)

Built on branch `claude/blissful-davinci-82py6w`, integrated into this app on `claude/phase4-integration`
(migration `0002`). Details: `docs/architecture/system-design.md`.

**Migration chain:** `0001` Phase 1 → `0002` Phase 4 core (`clients`, finance, SEO, reporting, `users.client_id`) →
`0003` Phase 2 (inventory, listings, publishing; `clients.simulated`, `users.client_role`) → `0004` interim Phase 3
tables → `0005` Phase 3 customer conversations (drops the `0004` tables). The revisions were renumbered during
integration (nothing had been deployed). `0004` stays in the chain so an existing database upgrades cleanly.

### Clients and the `client` role

* `clients` is the tenant boundary for dealership data. A won `Prospect` becomes a `Client`
  (`clients.prospect_id`, unique) via `services.tenancy.create_client()`, which also stops prospecting
  sequences and suppresses the address. Phase 2/3 tables that belong to a dealership must have
  `client_id → clients.id` (indexed, NOT NULL, `ON DELETE CASCADE`).
* Role `client` (lowest rank, `users.client_id` set) may only reach `/portal`, `/api/v1/portal/*` and its own
  Phase 2 workspace (`/dealers/{id}`, `/api/v1/dealers/{id}`); `web.deps.current_user` enforces this for every route.
  Two tenancy helpers exist on purpose: `services.tenancy.scope()/owned_or_404()` filter one query for portal code;
  `tenancy.tenant_session()/authorize_client()` scope a whole session (plus composite FKs) for Phase 2 workspaces. Portal code must use `services.tenancy.scope()` /
  `owned_or_404()` and must never take a client id from the request. Use `Depends(require_client)` for portal
  routes; `require("viewer")` and above never admit client users.
* Staff-side Phase 2/3 routes under `/clients/{cid}/…` must check that the row's `client_id` equals `cid`.

### Shared Phase 4 services other phases should use

| Need | Use |
|---|---|
| Record metered provider usage (AI tokens, API calls) | `services.usage.record_usage(db, provider=, category=, metric=, quantity=, ref=, client_id=)` (idempotent by `ref`) |
| Call Claude | `integrations.llm.complete()` (free text; raises `AIUnavailable` when off) or `structured_call()` (schema output; integration errors). Both record tokens per client and pass the emergency-stop gate |
| Email a client dealership | `services.client_email.send()` (gated; SIMULATED by default) |
| Open a ticket for a client-facing failure | `services.tickets.open_system_ticket(db, client_id=, kind="integration_failure", …, dedupe_key=)` |
| Raise an operator alert | `services.alerts.raise_alert(db, kind, message, severity, dedupe_key)` |
| Gate an automation on onboarding | add an entry to `services.onboarding.AUTOMATION_PREREQS`; check `automation_enabled(db, client_id, feature)` before acting (Phase 2 scheduled syncs do) |
| Fetch a user-supplied URL server-side | `services.netguard.assert_public_url()` (wraps the one SSRF check in `integrations.website`) on every request and redirect |
| Audit with an entity | `audit.record(db, action, actor=, entity=, details=)` (secret-named keys redacted) |

## Phase 3 additions: customer conversations

Ported from `claude/magical-darwin-nncp64` (`98fa3d3`) on `claude/phase3-integration`. Phase 3 had its own app,
auth, dealership and vehicle models; only its conversation domain was kept and mapped onto the shared models.

| Phase 3 original | In this app |
|---|---|
| `Dealership`, `BusinessHours`, `Membership` | `clients` (+ `public_chat_key`, `business_info_approved_at/_by_id`, `business_hours` JSON `{"0".."6": ["09:00","19:00"] or null}`), `users.client_role` |
| `Vehicle` (own inventory) | Phase 2 `vehicles` (+ `verified_at`, `data_conflict_note`); freshness = latest of `verified_at` / `last_seen_at`, and Phase 2's `stale` flag and `*_conflict` / implausible-value validation codes withhold prices |
| `IntegrationConnection` (own) | shared `integration_connections` (+ `external_account_id`, `paused`, `config`); customer channels use `kind="customer_messaging"`, the test-drive calendar `kind="calendar"` |
| Its sessions, MFA, password reset, audit HMAC chain | Not ported: Phase 1 login/CSRF/roles and `audit.record` |
| Its own LLM client | `integrations.llm.complete()` via an adapter, only when `DF_CONVERSATION_AI_ENABLED` and the AI master switch are on |
| Its notification email | Phase 4 `services.client_email.send()` (SIMULATED by default) behind the send gate |
| Its webhook-event table | Phase 4 `webhook_events` (ids only, never message text) |
| Pilot allowlist, live Messenger/Twilio senders, outreach agent | Not ported. Every customer channel is a sandbox adapter |

**Tables** (`models_conversations.py`, all `TenantMixin` with composite `(client_id, id)` keys): `customer_contacts`,
`customer_identities`, `customer_leads`, `contact_suppressions`, `conversations`, `conversation_messages`,
`response_drafts`, `escalations`, `appointments`, `appointment_events`, `response_automation_settings`,
`mode_authorizations`, `response_templates`, `dealer_faqs`, `notification_preferences`, `notifications`. Not
tenant-scoped: `evaluation_runs` (nullable `client_id`), `rate_counters`, `send_attempts` (send ledger).

**Services** (`services/conversations/`): `inbox` (ingest, dedupe, process), `responder/` (grounded EN/ES engine,
`ENGINE_VERSION` 1.0.0), `automation_policy` (Modes A/B/C), `outbound` (send job, final-content re-check),
`send_gate` (emergency stop + IN_FLIGHT/UNCERTAIN ledger), `rate_limits`, `escalation`, `summaries`, `appointments`,
`notifications`, `channels` (sandbox channels and calendar, webhook signature checks), `adf` (defusedxml),
`evaluation` (46 synthetic cases). Jobs: `conversations.process_message`, `.outbound_send`, `.notification_email`,
`.appointment_reminder`, `.dispatch_reminders`, `.daily_digests`.

**Routes:** dealership workspace `/dealers/{id}/inbox|conversations|appointments|replies|notifications` (permission
`respond` to act, `manage` for settings; Mode B/C needs one of the dealership's own `dealer_admin` users); agency
`/conversations`; dealer API `GET /api/v1/dealers/{id}/conversations[/{conv}]`, `POST …/conversations/{conv}/reply`,
`GET …/appointments`, `POST …/appointments/{id}/confirm`; public (no session, no CSRF, own checks) `/api/chat/{key}`,
`/chat/{key}`, `/webhooks/meta` (+ `/data-deletion`), `/webhooks/twilio/sms`, `/api/leads/adf/{connection_id}`.

**Phase 4 re-pointed:** `client_reports` count inquiries/appointments from `conversations`, `conversation_messages`,
`escalations` and `appointments` (SIMULATED excluded); the staff simulated-inquiry endpoint calls
`inbox.ingest_inbound(simulated=True)`; `maintenance.purge_conversation_pii` clears message/draft/appointment/contact
text; `maintenance.delete_client` deletes tenant rows explicitly, children first. `models_interim.py` and
`services/dealer_ops.py` were removed.

Secrets for Phase 3: `DF_META_APP_SECRET`, `DF_META_VERIFY_TOKEN` (env); a Twilio auth token is read from the
variable named in the connection's `config.auth_token_env`, which must start `DF_TWILIO_TOKEN_`; an ADF intake token
is shown once and only its SHA-256 is stored.
