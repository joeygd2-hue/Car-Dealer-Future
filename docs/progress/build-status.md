# Build status

## Milestone: architecture decision & reproducibility (2026-10-18)
- Checkpoint `e2d3030` verified (clean tree, 177 passed, ruff clean) before changes.
- Four foundations/branches built and tested in isolation; comparison + recommendation in
  `docs/architecture/foundation-decision.md` (**awaiting owner approval — nothing merged or deleted**).
- Clean-clone reproduction found and fixed 4 setup defects (blank `.env` boolean broke migrate/seed/121 tests;
  non-hermetic tests; missing `data/` dir broke `alembic upgrade`; seed could create an unversioned schema).
  Details and results: `docs/progress/reproducibility-report.md`.
- Repeated clean-clone runs exposed a real concurrency bug (R5: a duplicate job could mark a live send UNCERTAIN)
  — fixed in all three senders with a regression test proven to fail on the old code.
- Smoke script extended to 32 checks (roles, cross-client 404, stale-vehicle handling, final worker health);
  32/32 passed from the clean clone with web and worker as separate processes and a worker `kill -9` mid-run.

_Last updated: 2026-10-18 — Phase 3.5 (production readiness & dealership pilot). Phase 3 sections below are kept for history._

## Phase 3.5 summary (verified)
- **Repository state verified**: handoff claim of 112 tests confirmed (112 collected, 112 passed, 0 skipped) before changes.
  Discovered an unreported competing app (`dealerflow/`) on `claude/dealership-inventory-publishing-iq0aqw`; owner chose
  to harden this branch and port later (plan in `docs/architecture/shared-stack.md`).
- **Security**: TOTP MFA (+ admin enforcement in production), password reset/setup links, session invalidation, DB-backed
  login/MFA/reset/chat throttles, CSRF on logout, fail-fast config validation, CSP/HSTS/no-store, generic error page,
  HMAC-sealed append-only audit log with verifier, secrets never in URLs, redacted raw webhook storage, role matrix.
- **Worker/sending**: central send gate (platform emergency stop + pilot allowlist + `send_attempts` ledger), atomic
  claims, IN_FLIGHT-before-call + UNCERTAIN reconciliation (no silent duplicates), distributed rate/budget counters,
  revalidation immediately before every send, stale-worker detection.
- **Accuracy**: final outgoing text validated against current verified facts at send time; staff text checked with
  audited override; 24h freshness default for price/availability/mileage; ambiguity escalation; customer cancellation
  handling; reminders blocked after opt-out or appointment change.
- **Pilot mode**: Admin → Pilot console; documented sandbox pilot procedure in `docs/progress/pilot-readiness.md`.
- **Migration** `44a34c76e0bf` (server defaults for existing rows; seals pre-existing audit rows).

### Exact test results (Phase 3.5)
`uv run pytest` → **177 passed, 0 skipped, 0 xfail** (≈70 s). `ruff check .` → clean.
| File | Tests |
|---|---|
| test_accuracy_hardening.py | 10 |
| test_appointments_notifications.py | 7 |
| test_automation_modes.py | 9 |
| test_evaluation_and_adf.py | 4 |
| test_inbox_pipeline.py | 8 |
| test_jobs.py | 9 |
| test_language_analysis.py | 24 |
| test_migrations.py | 2 (subprocess Alembic: fresh upgrade + `alembic check`, upgrade over Phase 3 data, downgrade) |
| test_outbound_and_webhooks.py | 7 |
| test_outreach_agent.py | 24 |
| test_pilot_mode.py | 6 (live code path with **mocked** Meta HTTP — not a real provider) |
| test_portal_security.py | 11 |
| test_response_engine.py | 9 |
| test_role_matrix.py | 24 |
| test_security_hardening.py | 16 |
| test_worker_resilience.py | 7 |
Evaluation set: 46 synthetic EN/ES conversations, all metrics 1.0 (runs inside the suite).

Two Phase 3 tests were updated because behaviour intentionally changed: website-chat replies are now `SIMULATED`
until the chat channel is live-authorized (was `SENT`); outreach emergency stop now *holds* messages (was `BLOCKED`).

### Real-process verification (Phase 3.5)
Fresh SQLite DB → `alembic upgrade head` → seed → `uvicorn` and `python -m app.worker` as **separate processes** →
`scripts/smoke_e2e.py`: **24/24 checks passed**, including a `kill -9` of the worker mid-run and a replacement worker
taking over. Covered: EN + ES drafts from verified inventory, financing escalation without invented terms, test-drive
request recorded but not promised, STOP opt-out, staff send → SIMULATED and not delivered to the visitor, staff
appointment confirmation → SIMULATED confirmation, pilot console, emergency banner, emergency stop blocking a send,
ledger with zero live SENT rows, audit integrity OK (34 rows). Logs had no tracebacks.

**Still not verified:** any real provider account (none accessed), PostgreSQL, multi-host deployment.

## Starting state found
The repository contained only `README.md` (single "Initial commit"; no other branches or PRs). **No Phase 1 or Phase 2 code
existed.** With the owner's approval, Phase 3 created the shared FastAPI base described in `docs/architecture/shared-stack.md`,
including minimal inventory (Phase 2) and outreach/CRM (Phase 1) foundations that those phases should extend.

## Implemented (and how it was verified)

### Shared platform
| Feature | Status | Verification |
|---|---|---|
| FastAPI app, Jinja UI, health endpoints | done | page-render tests; real uvicorn smoke test |
| SQLAlchemy models (48 tables after Phase 3.5) + Alembic `4122abfb2c23` → `44a34c76e0bf` | done | migration tests |
| Auth: sessions, scrypt passwords, CSRF, login rate limit | done | tests |
| Tenant isolation (server-side on every route + object ownership) | done | cross-client HTTP tests |
| Encrypted credential storage (Fernet) | done | used by webhook tests |
| Audit log (every external action / state transition) | done | asserted across tests; portal + admin views |
| Durable job queue: idempotent enqueue, leases, retries w/ backoff, dead-letter, deferral, recurring scheduler, heartbeats | done | `tests/test_jobs.py`; real worker kill/restart smoke test |
| Integration registry with sandbox-by-default + live authorization gate | done | tests |

### Phase 3 — customer response, routing, portal
| Feature | Status | Verification |
|---|---|---|
| Messaging-access research | done | `docs/research/messaging-and-outreach-access.md` |
| Unified inbox (all required conversation fields) | done | tests |
| Channels: website chat, ADF/XML, Marketplace human-assisted; Messenger & Twilio adapters (code only) | see integration matrix | tests |
| Dedupe of webhook events and messages; provider ids + source timestamps kept | done | tests |
| CRM linking (contact, identity, lead) and vehicle linking (VIN/stock/model/context) | done | tests |
| Bilingual (EN/ES) grounded response engine; stale/missing/conflicting data → "needs confirmation" + escalation | done | engine tests + evaluation |
| Output validator (no unsupported numbers/claims/appointment promises/language mismatch); optional LLM rephrase guarded | done | tests |
| Modes A/B/C, authorization + passing-evaluation gate, global/dealer/channel pause, conversation takeover, rate limits, min interval, business hours, Messenger windows, confidence threshold, opt-out (EN+ES) | done | `tests/test_automation_modes.py`, pipeline tests |
| Escalation rules (financing, credit, warranty, trade-in, negotiation, condition, complaints, frustration, human request, missing/contradictory data, sensitive data — redacted before storage) | done | tests + evaluation |
| Summaries (all Step 7 fields) + recommended next step | done | tests |
| Appointments: business hours, calendar availability, staff confirmation, calendar event, confirmation/reminder/cancellation messages, reschedule, history | done (sandbox calendar) | tests |
| Notifications: in-app, email (SENT/SIMULATED/FAILED/SKIPPED shown truthfully), immediate alerts, daily digest, assigned-user + admin routing | done | tests |
| Client portal (inventory, listing approvals, inbox, assignment, appointments, hours, FAQs/templates, rules, notifications, mode, pause, audit, account, SEO placeholder) | done | page-render + action tests |
| Admin workspace (clients, integrations, global pause, live authorizations, evaluations, dead jobs/retry, usage, audit) | done | page-render tests |
| Evaluation set: 46 synthetic EN/ES conversations | done | **all metrics 1.0** (factual, escalation recall/accuracy, reasons, language, opt-out) |

### Persistent autonomous outreach agent (system-wide requirement)
| Feature | Status | Verification |
|---|---|---|
| State machine (STOPPED/RUNNING/PAUSED/EMERGENCY_STOPPED), bounded ticks, checkpoints, run generations | done | tests + real worker smoke test |
| Discovery (sandbox source, CSV import, Google Places code-only), dedupe, qualification, fit scoring, queue replenishment | done | tests |
| Evidence-backed facts (source URL, confidence, expiry) — drafts use recorded facts only | done | tests |
| Drafting with approval rules; CAN-SPAM footer + `ADV:` prefix + RFC 8058 one-click unsubscribe | done | tests |
| Pre-send compliance gate: emergency stop, state, channel (email only), approval, suppression (email/domain/phone), stop conditions, duplicate step, live-only guards, window, hourly/daily/campaign limits, budgets | done | tests |
| Inbox monitoring (sandbox + IMAP code), reply matching, EN/ES classification, stop on reply/meeting/opt-out/bounce, routine replies from approved FAQ, escalation to operator | done | tests |
| Meeting booking via public booking link → calendar + video (sandbox) → CONFIRMED; slot re-check | done | tests |
| Operator dashboard (measured vs simulated vs estimated, queue, costs, approvals, replies, prospects, suppression, activity log, sandbox tools) | done | page-render test + smoke test |

## Phase 3 test results (historical)
`uv run pytest` → **112 passed** (≈32 s). `ruff check app tests` → clean. Per file: appointments/notifications 7, automation modes 9,
evaluation/ADF 4, inbox pipeline 8, jobs 9, language/analysis 24, outbound/webhooks 7, outreach agent 24, portal security 11,
response engine 9. Evaluation: 46 cases, all metrics 1.0, passed.

Real-process smoke test (uvicorn + `python -m app.worker`, SQLite): chat API → draft (ES) and financing escalation; staff reply
delivered to widget poll; dealer blocked from `/admin` (403); outreach agent started from admin UI → sandbox discovery →
qualification → enrollment → 8 drafts pending approval; worker SIGTERM → heartbeat STOPPED → new worker resumed from DB state.

**Autonomous operation is verified only in sandbox.** No live provider has been called; see known-limitations.

## Next steps
0. **Decide the foundation** (`app/` vs `dealerflow/`) and execute the reconciliation plan before Phase 4.
1. Run the sandbox pilot (`docs/progress/pilot-readiness.md`). Only with explicit approval: one live test to allowlisted internal recipients.

## Earlier next steps (Phase 3 handoff)
1. Read `docs/architecture/shared-stack.md`; extend existing models via Alembic migrations.
2. Phase 2: replace the minimal manual inventory form with ingestion (keep `verified_at` / `data_conflict_note` semantics) and generate `ListingDraft` rows; the portal approval UI already exists.
3. Phase 1: extend the outreach CRM (prospects/campaigns/sales_meetings) rather than creating new tables; outreach agent runbook is in this file's section above and `/admin/outreach`.
4. Phase 4: add finance/SEO/analytics models; surface SEO reports in `/portal/{id}/account` (placeholder exists); reuse `cost_entries`; run a security review (CSP header, shared rate-limit store, PostgreSQL migration test, secrets rotation).
