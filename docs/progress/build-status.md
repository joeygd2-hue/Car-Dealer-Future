# Build status

_Last updated: 2026-10-18 (Phase 3 chat; also built the shared platform base and the persistent outreach agent at the owner's request)._

## Starting state found
The repository contained only `README.md` (single "Initial commit"; no other branches or PRs). **No Phase 1 or Phase 2 code
existed.** With the owner's approval, Phase 3 created the shared FastAPI base described in `docs/architecture/shared-stack.md`,
including minimal inventory (Phase 2) and outreach/CRM (Phase 1) foundations that those phases should extend.

## Implemented (and how it was verified)

### Shared platform
| Feature | Status | Verification |
|---|---|---|
| FastAPI app, Jinja UI, health endpoints | done | page-render tests; real uvicorn smoke test |
| SQLAlchemy models (44 tables) + Alembic baseline `4122abfb2c23` | done | `alembic upgrade head` + `alembic check` clean |
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

## Test results (actual)
`uv run pytest` → **112 passed** (≈32 s). `ruff check app tests` → clean. Per file: appointments/notifications 7, automation modes 9,
evaluation/ADF 4, inbox pipeline 8, jobs 9, language/analysis 24, outbound/webhooks 7, outreach agent 24, portal security 11,
response engine 9. Evaluation: 46 cases, all metrics 1.0, passed.

Real-process smoke test (uvicorn + `python -m app.worker`, SQLite): chat API → draft (ES) and financing escalation; staff reply
delivered to widget poll; dealer blocked from `/admin` (403); outreach agent started from admin UI → sandbox discovery →
qualification → enrollment → 8 drafts pending approval; worker SIGTERM → heartbeat STOPPED → new worker resumed from DB state.

**Autonomous operation is verified only in sandbox.** No live provider has been called; see known-limitations.

## Next steps for the next phase (Phase 4) — and for Phase 1/2 chats if they continue
1. Read `docs/architecture/shared-stack.md`; extend existing models via Alembic migrations.
2. Phase 2: replace the minimal manual inventory form with ingestion (keep `verified_at` / `data_conflict_note` semantics) and generate `ListingDraft` rows; the portal approval UI already exists.
3. Phase 1: extend the outreach CRM (prospects/campaigns/sales_meetings) rather than creating new tables; outreach agent runbook is in this file's section above and `/admin/outreach`.
4. Phase 4: add finance/SEO/analytics models; surface SEO reports in `/portal/{id}/account` (placeholder exists); reuse `cost_entries`; run a security review (CSP header, shared rate-limit store, PostgreSQL migration test, secrets rotation).
