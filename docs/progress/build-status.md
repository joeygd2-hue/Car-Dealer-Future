# Build status

| Phase | Scope | Status |
|---|---|---|
| **1** | Research, lead generation, CRM, sales operations, persistent outreach agent | **Implemented and tested with simulated providers.** Live integrations not yet verified (no credentials). |
| 2 | (defined in its own task) | Not started. Must extend this app; see `docs/architecture/shared-stack.md`. |
| 3 | (defined in its own task) | Not started. |
| 4 | (defined in its own task) | Not started. |

Last updated: 2026-10-18 · branch `claude/charming-ritchie-358e1g`

## Starting point

The repository held only a one-line `README.md` (commit `163a6cd`). There was no earlier code, schema,
auth, tests or deployment to keep, so Phase 1 set the stack for every phase: Python + FastAPI +
SQLAlchemy/Alembic + Jinja, matching the owner's other Python/FastAPI project. Details are in
`docs/architecture/shared-stack.md`.

## Phase 1: what's implemented

| Area | Implemented | Where |
|---|---|---|
| Research docs | Feasibility report, source register, integration matrix | `docs/research/`, `docs/architecture/integration-matrix.md` |
| Lead discovery | Fixture (SIMULATED), OpenStreetMap Overpass, Google Places (New) Text Search, dealer website research (robots.txt-aware, off by default), CSV/XLSX import with dry run, manual entry, JSON API | `integrations/discovery.py`, `integrations/website.py`, `services/importer.py`, `services/discovery.py` |
| Data quality | Validation (URL, email, US phone→E.164, state, ZIP), missing-field indicators, per-field evidence with source URL and timestamp, conflicts kept as evidence (never overwritten), last-verified date | `services/normalize.py`, `services/prospects.py` |
| Deduplication | Source ID → domain → phone → email domain → fuzzy name with same ZIP/city/≤0.5 mi; manual merge | `services/prospects.py` |
| Scoring | 7 weighted factors (configurable weights and threshold), per-factor reason, evidence IDs, unverified list; DNC scores 0; no revenue/volume/demographic estimates | `services/scoring.py` |
| CRM | 16 configurable stages; workflow-only stages (approved/contacted/replied/booked) can't be set by hand; timeline; tasks with dedupe keys; proposals; calls; notes; verified contacts; filters, search, sort, pagination; bulk actions (≤200, typed confirmation, audit) | `services/crm.py`, `web/routes_crm.py` |
| Outreach | EN/ES editable templates; whitelisted variables from verified data only; CAN-SPAM footer + RFC 8058 one-click unsubscribe; manager approval; pre-send compliance gate re-checked at send; suppression (email/@domain/phone); cooldown, touch cap, daily cap (deferred), duplicate check; states draft→queued→sending→sent/replied/bounced/failed/cancelled; ambiguous sends never resent | `services/outreach.py`, `services/compliance.py` |
| Inbox | Mock, SMTP/IMAP (read-only), Gmail API; idempotent import; matching by In-Reply-To/References → thread → recipient → contact → domain; EN/ES rule classifier; opt-outs auto-suppressed; bounces suppressed; stop conditions; follow-up tasks; unmatched queue | `services/inbox.py`, `services/classify.py` |
| Meetings | Real free/busy → suggested slots → choice → manager approval → Zoom then calendar (checkpointed, idempotent) → `booked` only after both succeed; re-check before booking; reschedule, cancel, complete/no-show; Zoom webhooks | `services/meetings.py`, `integrations/zoom.py`, `integrations/calendar.py` |
| Calling | Manual tel: link (default) or Twilio agent-first bridge; calling-hours check; DNC/suppression check; outcome logging; opt-out by phone | `integrations/calls.py`, `web/routes_crm.py` |
| Autonomous agent | Worker + DB job queue, heartbeat, time-bucketed idempotent ticks, 8 bounded steps with checkpoints, run log, stopped/running/paused/emergency-stop, admin live authorization, campaigns with auto/manual approval, auto-answers from approved knowledge only, budgets, operating hours, replenishment | `services/agent.py`, `worker.py`, `jobs.py`, `/agent` |
| Reporting | Daily dashboard (real vs simulated separated), weekly report (funnel, source performance, meeting conversion, CAC when calculable, recommendations), integration health, failed jobs, optional daily digest email | `services/reports.py`, `services/notifications.py` |
| Platform | Session login, roles (viewer/rep/manager/admin), CSRF, login rate limit, per-user API keys, JSON API, audit log, integration log, JSON logs, HTTP retries/backoff/rate limits, Alembic migrations, Docker/Compose, health check | `web/`, `audit.py`, `integrations/http.py`, `migrations/` |

## Tests

`pytest` runs 99 tests in about 20 s with no network. The same suite passes on Postgres 16
(`DF_TEST_PG_URL=postgresql+psycopg://… pytest`).

| File | Tests | Covers |
|---|---|---|
| `test_workflow.py` | 1 | **Step 9 scenario:** fixture import → dedup → score → draft → approve → mock send → reply matched → CRM stage + tasks → availability → approval → Zoom + calendar booking → reschedule → cancel |
| `test_compliance.py` | 13 | Opt-out by reply (EN/ES) and link (incl. one-click POST), domain suppression, opt-out between approval and send, duplicate jobs send once, cooldown/duplicate/touch limits, stop on reply, missing CAN-SPAM identity, rep can't approve, missing verified values, unverified observations, daily-cap deferral |
| `test_failures.py` | 15 | Rate-limit retry with backoff, expired credentials fail fast, provider rejection, interrupted send not resent, stale-lock recovery, exclusive claim, Zoom failure leaves meeting unbooked, calendar failure retry reuses Zoom meeting, availability failure offers no times, HTTP 5xx/401/429/400 mapping, Google `invalid_grant`, missing credentials |
| `test_agent.py` | 16 | Stopped/paused/emergency-stop behaviour, replenish → score → enroll → draft, auto mode in simulation, auto mode needs admin, real provider never sends without live authorization + env flag, budget limit, discovery-per-day limit, campaign daily cap, step isolation, approved-answers-only with sensitive escalation, idempotent tick scheduling, restart recovery, health/heartbeat |
| `test_web.py` | 13 | Full UI workflow over HTTP (Spanish draft → send → simulated reply → booking) rendering every detail page, anonymous access, login rate limit, every page renders, CSRF, viewer/rep restrictions, workflow-only stages via API, API keys, bulk-action safeguards, Zoom webhook signature, link/redirect safety |
| `test_prospecting.py` | 8 | Validation, CSV dry run + commit + dedup, XLSX, conflicting values, fuzzy-name dedup, manual merge, explainable/configurable scoring, missing fields |
| `test_providers_http.py` | 10 | Request/response shape for Gmail, Google Calendar (incl. 409 idempotency), Zoom S2S, Places, Overpass, website research (robots + signals), SMTP, Twilio, against mocked HTTP |
| `test_classify.py` | 20 | EN/ES classification cases, quoted text ignored, mixed signals, header-based auto-reply/bounce |
| `test_reports.py` | 2 | Drafts ≠ sent, simulated counted separately, simulated sends don't mark real prospects contacted |
| `test_migrations.py` | 1 | Migrations match models exactly |

Also verified by hand: `migrate` → `create-user` → `demo` → `web` → `run-jobs` → `/healthz` shows a live worker heartbeat.
Migration upgrade and downgrade were run on SQLite and Postgres 16.

## Not verified (see known-limitations.md)

No live provider calls were made during the build. "Implemented" above means "code exists and passes
tests with mocks". It does not mean the integration works with a real account.

## How to test locally

```bash
pip install -e ".[dev]" && pytest
python -m dealerflow migrate && python -m dealerflow create-user you@example.com admin
python -m dealerflow demo && python -m dealerflow web    # and `python -m dealerflow worker`
```
In the UI: Prospects → open a SIMULATED dealer → Draft email → Approve and send now → Inbox → Simulate a reply
asking for a Zoom call → Meetings → Check calendar → Choose → Approve and book. Agent page: Start, Run one tick now,
Pause, Emergency stop.

## Next steps for the owner

1. Fill in sender identity (`DF_BUSINESS_*`, `DF_SENDER_*`) and edit templates and approved answers so every statement is true.
2. Pick an email route (Google Workspace via Gmail API or SMTP) and set up SPF, DKIM and DMARC on your sending domain.
3. Create the Google Calendar and Zoom S2S credentials; run Integrations → Verify credentials.
4. Decide on lead sources (see feasibility report); import a first real list by CSV.
5. Send 5–10 human-approved emails, then consider enabling the agent in `manual` approval mode.
