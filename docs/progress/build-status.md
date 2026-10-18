# Build status

| Phase | Scope | Status |
|---|---|---|
| **1** | Research, lead generation, CRM, sales operations, persistent outreach agent | **Implemented and tested with simulated providers.** Live integrations not yet verified (no credentials). |
| **1.5** | Launch-readiness audit | **Done 2026-10-18.** Verdict: not ready for live outreach; blockers in `docs/audit/launch-readiness-audit.md` §7. |
| 2 | Inventory, listings, publishing | In progress on `claude/dealership-inventory-publishing-iq0aqw` (porting into dealerflow). Phase 4 ships interim tables it must replace (`shared-stack.md`). |
| 3 | Customer response, conversations, portal | On `claude/magical-darwin-nncp64` as a **separate `app` package — not yet ported into dealerflow**. |
| **4** | Finance, expenses, usage costs, SEO audits, client reporting, service catalog, onboarding, tickets, client portal, security review | **Implemented and tested with simulated providers** on branch `claude/blissful-davinci-82py6w` (merged with this Phase 1 work). Live integrations not verified. |

Last updated: 2026-10-18 · Phase 1 on `claude/charming-ritchie-358e1g`; Phase 4 (this file's latest edit) on `claude/blissful-davinci-82py6w`

## Starting point

The repository held only a one-line `README.md` (commit `163a6cd`). There was no earlier code, schema,
auth, tests or deployment to keep, so Phase 1 set the stack for every phase: Python + FastAPI +
SQLAlchemy/Alembic + Jinja, matching the owner's other Python/FastAPI project. Details are in
`docs/architecture/shared-stack.md`.

## Phase 1: what's implemented

| Area | Implemented | Where |
|---|---|---|
| Research docs | Feasibility report (lead sources, email, calling, Texas law, privacy, costs, unresolved questions), source register with access dates and confidence, integration matrix | `docs/research/`, `docs/architecture/integration-matrix.md` |
| Lead discovery | Fixture (SIMULATED), OpenStreetMap Overpass (with ODbL attribution), CSV/XLSX import with dry run (incl. TxDMV-style license columns), manual entry, JSON API, dealer website research (robots.txt-aware, off by default). Google Places is **disabled for stored leads** (terms allow storing place IDs only) | `integrations/discovery.py`, `integrations/website.py`, `services/importer.py`, `services/discovery.py` |
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

`pytest` runs 136 tests in about 30 s with no network (102 at the end of Phase 1; 34 added by the audit, including
`test_adversarial.py`, `test_txdmv.py`, `test_backup.py`, `test_livecheck.py`). The same suite passes on Postgres 16
(`DF_TEST_PG_URL=postgresql+psycopg://… pytest`).

| File | Tests | Covers |
|---|---|---|
| `test_workflow.py` | 1 | **Step 9 scenario:** fixture import → dedup → score → draft → approve → mock send → reply matched → CRM stage + tasks → availability → approval → Zoom + calendar booking → reschedule → cancel |
| `test_compliance.py` | 13 | Opt-out by reply (EN/ES) and link (incl. one-click POST), domain suppression, opt-out between approval and send, duplicate jobs send once, cooldown/duplicate/touch limits, stop on reply, missing CAN-SPAM identity, rep can't approve, missing verified values, unverified observations, daily-cap deferral |
| `test_failures.py` | 15 | Rate-limit retry with backoff, expired credentials fail fast, provider rejection, interrupted send not resent, stale-lock recovery, exclusive claim, Zoom failure leaves meeting unbooked, calendar failure retry reuses Zoom meeting, availability failure offers no times, HTTP 5xx/401/429/400 mapping, Google `invalid_grant`, missing credentials |
| `test_agent.py` | 17 | Stopped/paused/emergency-stop behaviour, replenish → score → enroll → draft, auto mode in simulation, auto mode needs admin, real provider never sends without live authorization + env flag, budget limit with estimated costs, Google Places refused, discovery-per-day limit, campaign daily cap, step isolation, approved-answers-only with sensitive escalation, idempotent tick scheduling, restart recovery, health/heartbeat |
| `test_web.py` | 13 | Full UI workflow over HTTP (Spanish draft → send → simulated reply → booking) rendering every detail page, anonymous access, login rate limit, every page renders, CSRF, viewer/rep restrictions, workflow-only stages via API, API keys, bulk-action safeguards, Zoom webhook signature, link/redirect safety |
| `test_prospecting.py` | 9 | Validation, CSV dry run + commit + dedup, XLSX, conflicting values, fuzzy-name dedup, manual merge, explainable/configurable scoring, missing fields, license-list columns kept as evidence |
| `test_providers_http.py` | 11 | Request/response shape for Gmail, Google Calendar (incl. 409 idempotency), Zoom S2S, Places, Overpass, website research (robots disallow, unreachable robots.txt, signals), SMTP, Twilio, against mocked HTTP |
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

## Phase 1.5 additions

- Central integration gate (emergency stop and disabled providers), campaign and agent send holds, Zoom lost-response recovery,
  bounce matching, SSRF guard, CSRF on the JSON API, CSP/HSTS, password-bound sessions, file-based secrets.
- TxDMV `.xls` import profile built against the real public file.
- `backup`, `restore`, `retention` and `live-check` commands.
- Audit documents in `docs/audit/`.

## Next steps for the owner

1. Fill in sender identity (`DF_BUSINESS_*`, `DF_SENDER_*`) and edit templates and approved answers so every statement is true.
2. Pick an email route (Google Workspace via Gmail API or SMTP) and set up SPF, DKIM and DMARC on your sending domain.
3. Create the Google Calendar and Zoom S2S credentials; run Integrations → Verify credentials.
4. Download the TxDMV *Motor Vehicle Dealers List* and import it (dry run first). Google Places is not usable for stored leads.
5. Before a cold-calling routine, settle the Texas Chapter 302 question (feasibility report §4).
6. Send 5–10 human-approved emails, then consider enabling the agent in `manual` approval mode.


## Phase 4: what's implemented

Branch `claude/blissful-davinci-82py6w`. Phase 4 began before Phase 1 was pushed, built a parallel foundation,
and then — on the owner's decision — was **re-implemented inside dealerflow**; the parallel `app` package was
removed (still in history at `fdfa4f4`).

| Area | Implemented | Where |
|---|---|---|
| Tenancy | `clients` (1:1 with won prospects), `client` role confined to the portal centrally, scoped queries, portal users managed per client | `models_ops.py`, `services/tenancy.py`, `web/deps.py`, `web/routes_portal.py` |
| Finance | Invoices (draft/issued/void/written off; paid state derived), lines (setup/recurring/one-time/discount), payments (only confirmed = cash), credits and refunds, agreements with copied prices and discounts, period invoicing, MRR/ARR, receivables aging, revenue by client, CAC / cost per qualified lead / per booked meeting (or "not measurable"), retention/churn, ARPC, client costs and partial gross margin, cash-flow actuals + labelled forecast, hypothetical break-even, CSV exports, bank-CSV reconciliation, PAN rejection, tax disclaimer | `services/finance.py`, `web/routes_finance.py` |
| Expenses | Date, vendor, configurable categories (11 defaults incl. AI, software, hosting, data, outreach, ads, domains, contractors, phone, payment processing), amount+currency, purpose, recurrence, client/project, reimbursement, notes, receipt upload, source=manual | `services/finance.py`, `services/maintenance.py` |
| Usage costs | `cost_entries` extended with provider/metric/quantity/client and verified/estimated/recorded/unknown basis; Claude token recording; billing CSV import; budgets with threshold, limit and spike alerts (hourly job) | `services/usage.py`, `integrations/llm.py` |
| Payments integration | Stripe webhook: signature, tolerance, dedup, job processing, failure alerts | `integrations/stripe.py` |
| Service catalog | 3 proposed packages at $0 placeholders; editable prices/frequency/features; package inclusion | `services/packages.py` |
| Onboarding | 14-step checklist with evidence, activation gate, per-automation prerequisites (steps + verified integrations), tickets (manual/portal/system) | `services/onboarding.py`, `services/tickets.py`, `web/routes_clients.py` |
| SEO | Authorized sites only; robots-aware polite crawler (stops on 429, GET/HEAD only); checks for titles, descriptions, duplicates, noindex, sitemap/robots, headings, alt text, JSON-LD validity, AutoDealer/LocalBusiness schema, NAP consistency, inventory (Vehicle schema, price), broken internal links, orphan pages, Spanish-content opportunity, PageSpeed (optional); measured vs heuristic labels; issue lifecycle with before/after across audits; Search Console CSV import | `services/seo_*.py`, `web/routes_seo.py` |
| Client reports | Immutable monthly snapshots with sources, channel status, simulated exclusion, attribution disclaimer; publish to portal; download/print; email via gated client email; monthly drafting job | `services/client_reports.py`, `services/client_email.py` |
| Phase 2/3 interim | CSV inventory import, EN/ES template listings with approval, publishing recorded as `manual_required`, simulated inquiries, approved bilingual responses, lead summaries to the dealership | `models_interim.py`, `services/dealer_ops.py` |
| Hardening | See `docs/security/threat-model.md` (7 issues fixed) | `web/app.py`, `web/deps.py`, `static/app.js` |
| Operations | Retention purge, client export and confirmed deletion, backups/restore runbook, deployment guide | `services/maintenance.py`, `docs/operations/` |

### Phase 4 tests (run 2026-10-18)

`pytest` → **175 passed** (102 Phase 1 + 73 Phase 4) in ~85 s on SQLite; **175 passed** on Postgres 16
(`DF_TEST_PG_URL=…`), no warnings. `pip-audit`: no known vulnerabilities.

| File | Cases | Covers |
|---|---|---|
| `test_e2e_business_journey.py` | 1 | **Step 8 journey, all 14 steps** in one run: fixture discovery → approved outreach (mock send) → reply matched → meeting booked (mock calendar + Zoom) → won → client created → inventory → EN/ES listings approved → publishing `manual_required` → Spanish inquiry + approved Spanish reply → summary to the right dealership → revenue/expenses → SEO audit → client report reflects data → cross-client access blocked |
| `test_phase4_security.py` | 39 | Portal isolation (lists + direct ids, both directions), client confinement on 15 staff paths, role matrix, CSRF on cookie JSON, open redirects, headers/CSP/no inline script, API rate limit, PAN rejection + log redaction, Stripe signature/tolerance/dedup/disabled, upload validation, production config, audit scrubbing, unbound/converted client users, portal password reset scoping |
| `test_phase4_finance.py` | 17 | Invoice ≠ cash, drafts ≠ revenue, pending ≠ cash, payment rules, void/credit/refund, discount limits, agreements/MRR/ARR/period invoicing/churn, unmeasurable CAC, CAC + margin, CSV export, reconciliation, break-even, cash-flow labels, Stripe payment once, usage bases + idempotency + billing import, budget/spike alerts (deduped), AI gateway off by default + token recording, finance UI forms |
| `test_phase4_seo_reports.py` | 12 | Authorization required, evidenced findings, measured vs heuristic, robots honoured, re-audit resolution with before/after, 429 stop, worker job path, read-only crawling, onboarding/automation gating, report contents/channels/attribution/Search Console, simulated email, real provider not used without authorization, monthly job idempotency, lead summary routing |
| `test_phase4_pages.py` | 4 | Every Phase 4 page renders for admin/manager/viewer/portal with role-appropriate controls |

Also run by hand: `migrate` → admin user → `web` (uvicorn) → `run-jobs` → login → every Phase 4 page (200, CSP
present) → `/healthz` shows a live worker. Migrations `0002`/`0003`: upgrade → downgrade → upgrade with
existing rows on SQLite and Postgres 16.

### Next steps

1. **Phase 2** (in progress): replace the interim tables (`shared-stack.md` → "Phase 4 interim tables"), wire
   real inventory/listing counts into `client_reports.build_report_data()`.
2. **Phase 3**: port the separate `app` package on `claude/magical-darwin-nncp64` into dealerflow, using
   `clients`, the `client` role, `tenancy.scope()` and the shared job queue; replace interim conversations.
3. Integrate the branches (Phase 1 → 2 → 3 → 4) into `main` in that order, re-running the full suite after each.
4. Verify live integrations one at a time with test accounts (`known-limitations.md`).
