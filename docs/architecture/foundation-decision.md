# Foundation decision: one canonical application

_Prepared 2026-10-18 on `claude/magical-darwin-nncp64`. **Recommendation only — no merge, deletion or
force-push has been performed. Awaiting owner approval.**_

## 1. What exists (verified by building and testing each branch in isolation)

| Branch (head) | Package | Phases | Tests (exact, this run) | Migrations | Tables | Lint |
|---|---|---|---|---|---|---|
| `claude/magical-darwin-nncp64` (this milestone) | `app/` | 3 + 3.5 (+ a Phase-1-style outreach agent built on request) | **184 passed** (an intermittent failure was traced to a real concurrency race and fixed — reproducibility report R5) | `4122abfb2c23` → `44a34c76e0bf` | 48 | ruff clean |
| `claude/charming-ritchie-358e1g` (`427b137`) | `dealerflow/` | 1 (+ "1.5" launch-readiness hardening) | **136 passed**, 0 skipped | `0001` | 25 | none configured (ruff: 2 core errors) |
| `claude/dealership-inventory-publishing-iq0aqw` (`f174a06`) | `dealerflow/` | 1 (older) + 2 | **146 passed**, 0 skipped | `0001` → `0002` (phase 2) | 39 | none (2 core errors) |
| `claude/blissful-davinci-82py6w` (`c13f022`) | `dealerflow/` | 1 (older) + 4 + interim 2/3 stand-ins | **174 passed, 1 failed** with `[dev]` (test needs optional `anthropic`); 175/175 with `[dev,ai]` | `0001` → `0002` (phase 4 core) → `0003` (interim 2/3) | 56 | none |

None of the four contains another's latest work, and nothing is on `main`. Three of the four chats built on
`dealerflow`; this branch is the outlier.

## 2. Feature-by-feature comparison

Legend: ✅ implemented + tested · ◐ partial / interim · ✗ absent · **bold** = strongest implementation.

| Feature | `app/` (P3/3.5) | dealerflow P1 | dealerflow P2 | dealerflow P4 | Strongest | Port |
|---|---|---|---|---|---|---|
| Prospect discovery, import, dedupe, evidence, scoring, TxDMV | ◐ (sandbox source, CSV, simple dedupe) | **✅ (evidence model, 7-factor scoring, TxDMV, fuzzy dedupe)** | ✅ (older) | ✅ (older) | P1 | keep P1; retire `app/` outreach |
| CRM pipeline, tasks, proposals, calls | ✗ | **✅** | ✅ | ✅ | P1 | — |
| Outreach email, sequences, reply classification, suppression | ✅ | **✅ (manager approval, touch caps, cooldowns, knowledge base)** | ✅ | ✅ | P1 + `app/` send-safety | port send ledger/uncertain/limits into P1 |
| Autonomous outreach agent (pause, e-stop, live authorization) | ✅ | **✅ (17 + adversarial tests, livecheck)** | ✅ (older) | ✅ (older) | P1 | merge ideas, not code |
| Meetings (Zoom + Google Calendar) | ✅ (booking link) | **✅ (approval, idempotent recovery)** | ✅ | ✅ | P1 | — |
| Client/dealership tenancy | ✅ `dealerships` + memberships, `get_owned` 404s | ✗ (single business) | **✅ `clients` + composite (client_id, id) FKs + `tenant_session` loader criteria + flush guard** | ✅ `clients` + `tenancy.scope()` | P2 | P2 model + P4 columns |
| Dealer/client users & portal | ✅ dealer admin/staff portal | ✗ | ✅ dealer_admin/reviewer/viewer, `/dealers/{id}` | ✅ role `client`, `/portal` (reports, invoices, tickets) | P2 roles + P4 portal confinement | reconcile |
| Inventory ingestion (CSV/XLSX/feeds), VIN check, change reactions | ◐ manual form only | ✗ | **✅** | ◐ interim CSV | P2 | — |
| Freshness / verification | **✅ `verified_at` + conflict note; 24h policy; send-time re-validation** | ✗ | ✅ `last_seen_at` + stale scan (72h), no human-verified field | ◐ | P2 data + `app/` policy | add `verified_at` to P2 vehicles |
| Listing generation EN/ES, guardrails, approvals, publishing | ◐ approve/reject only | ✗ | **✅ (revisions, guardrails, Meta catalog code, manual Marketplace)** | ◐ interim | P2 | — |
| Customer inbox (chat, ADF, Marketplace-assisted, Messenger, Twilio) | **✅** | ✗ | ✗ | ◐ interim conversations/messages | `app/` | port |
| EN/ES grounded response engine + evaluation set (46 cases) | **✅** | ✗ | ✗ | ✗ | `app/` | port (pure services) |
| Automation modes A/B/C, escalations, summaries | **✅** | ✗ | ✗ | ◐ gates only | `app/` | port; rename table |
| Test-drive appointments | **✅** | ✗ | ✗ | ◐ interim | `app/` | port |
| Finance, invoices, expenses, reconciliation, Stripe webhook | ✗ | ✗ | ✗ | **✅** | P4 | — |
| SEO audit, client reports, tickets, onboarding | ✗ | ✗ | ✗ | **✅** | P4 | — |
| Durable jobs | ✅ leases, RetryLater, scheduler, heartbeats | ✅ atomic claim, DeferJob, failure hooks | ✅ | ✅ | tie; keep dealerflow's, add heartbeat/stale-worker view | small |
| Duplicate-send protection | **✅ ledger IN_FLIGHT → UNCERTAIN reconciliation, uncertain-result classification** | ✅ commit `sending` first, re-run marks failed | ✅ | ✅ | `app/` (operator reconciliation, all channels) | port |
| Distributed rate limits / budgets | **✅ DB counters (multi-worker safe)** | ◐ in-process; single worker assumed | ◐ | ◐ | `app/` | port |
| Pilot mode (allowlist, e-stop all sends, return-to-sandbox, console) | **✅** | ◐ livecheck test inboxes + agent e-stop | ◐ | ◐ | `app/` | port |
| MFA, password reset, server-side session revocation | **✅ TOTP + recovery codes, reset/setup links, server-side sessions** | ✗ MFA/reset; password-version revocation | ✗ | ✗ | `app/` | port (largest auth change) |
| Audit log integrity | **✅ HMAC per row + gap detection + append-only** | ✅ audit + integration_log (no tamper evidence) | ✅ | ✅ | `app/` + dealerflow's integration_log | port HMAC |
| SSRF protection | n/a (no fetching) | ✅ (DNS-rebinding gap) | ✅ netguard (feeds only) | ✗ for SEO crawler | P1/P2 (unify) | fix during merge |
| Secrets | env + Fernet-encrypted per-tenant credentials | env/secret files (`SecretStr`), nothing in DB | same | same | combine | — |
| Deploy | none (docs only) | **Dockerfile + compose (never built)** | same | same | dealerflow | — |

## 3. Database conflicts
| Conflict | Where | Resolution |
|---|---|---|
| `clients` defined twice (P2: slug, simulated, timezone; P4: legal name, address, phone, website, hours, languages) | P2 `0002` vs P4 `0002` | Keep P2 table (stronger isolation); add P4 columns in a new migration |
| `vehicles`, `listings` defined twice; P4 adds `inventory_imports`, `publish_attempts` | P2 `0002` vs P4 `0003` (interim) | Keep P2; drop P4 interim tables; rewire P4 `dealer_ops`, `client_reports`, `maintenance`, portal to P2 tables |
| Two different revision `0002`s | P2 vs P4 | New linear chain on the integration branch: `0001` → `0002` (P2) → `0003` (P4 core, regenerated without `clients`) → `0004` (Phase 3 port) → `0005` (3.5 safety) |
| `users`: P2 `client_id` + `client_role`; P4 `client_id` + role `client`; `app/` uses memberships (multi-dealer users) | all | P2 `client_role` + P4 portal confinement; one dealer per user (drop `app/` multi-membership) |
| `conversations`, `appointments` (P4 interim) vs `app/` | P4 `0003` vs `app/` | Replace with `app/` schema (`client_id` instead of `dealership_id`) |
| `automation_settings`: P4 = per-client automation gates; `app/` = response-mode settings | P4 `0002` vs `app/` | Rename `app/` table to `response_automation_settings` |
| `integration_connections` (P4 vs `app/`) | | Keep P4 table, add `encrypted_credentials`, `paused`, `config` columns |
| Outreach tables (`prospects`, `campaigns`, `outreach_messages`, `suppressions`, `cost_entries`, `jobs`, `users`) duplicated by `app/` | `app/` vs P1 | Keep P1; do not port `app/` outreach tables |

## 4. API / route conflicts
- Both: `/login`, `/logout`, `/healthz`, `/u/{token}` (unsubscribe), `/admin`-style operator pages. dealerflow wins; `app/` routes move under its routers.
- Portals: `app/` `/portal/{dealership_id}/…`, P2 `/dealers/{id}/…`, P4 `/portal` (client-scoped, no id). Recommendation: dealer staff UI under P2's `/dealers/{id}` (add Inbox, Appointments, Automation, FAQs tabs from `app/`); P4 `/portal` stays for client reports/invoices/tickets.
- Public endpoints to port unchanged: `/api/chat/{key}/messages`, `/chat/{key}`, `/webhooks/meta`, `/webhooks/meta/data-deletion`, `/webhooks/twilio/sms`, `/api/leads/adf/{id}`, `/book/{token}` (only if `app/` booking is kept — P1 has its own meetings flow), `/password/*`, `/login/mfa`, `/account/security`, `/admin/pilot`.
- Sessions: dealerflow uses Starlette signed cookies; `app/` uses server-side session rows (needed for MFA-pending state and revocation). Adopt server-side sessions.
- Env vars: dealerflow uses `DF_` prefix; `app/` uses none. Adopt `DF_`.

## 5. Migration and data-loss risks
- **No production data exists on any branch** (all synthetic/sandbox). Deciding now is the cheapest moment.
- Dropping P4 interim tables loses only synthetic rows; any database already at P4 `0003` needs a bridging migration (none known to be deployed — confirm).
- If a sandbox pilot runs on `app/` before the port, its staff-entered config (dealership profile, hours, vehicles, FAQs/templates, users) needs a one-time export/import script (≈0.5 day). Conversations from a sandbox pilot can be archived rather than migrated.
- Auth change (signed cookies → server-side sessions, MFA) logs every user out once; plan it.
- Audit HMAC seals are keyed by `ENCRYPTION_KEY`; existing dealerflow audit rows would be sealed at migration time (as done here in `44a34c76e0bf`).
- Risk of silently weakening guarantees during the port → mitigated by porting `app/`'s tests (they encode the pilot guarantees) and requiring them to pass before `app/` is retired.

## 6. Recommendation
**Make `dealerflow` the canonical application, built on `claude/charming-ritchie-358e1g` (latest Phase 1).
Merge Phase 2, then Phase 4, then port Phase 3 and the Phase 3.5 safety layer from `app/` into it.**

Why:
1. Three of four phases already live in `dealerflow`; porting Phase 3 (one phase, mostly pure services) is far smaller than porting Phases 1, 2 and 4 into `app/` (≈90 tables of mature functionality).
2. dealerflow's Phase 1 CRM/outreach and Phase 2 inventory/tenant isolation are stronger than `app/`'s equivalents.
3. `app/`'s unique value — the customer-response engine, evaluation set, pilot controls, send ledger, distributed limits, MFA/reset, audit HMAC — is modular and portable.

Alternative considered — `app/` as canonical: rejected. It would discard or re-port the work of three chats and still need P2's stronger tenancy.

## 7. Safe integration plan (no force-pushes, nothing deleted)
| Step | Action | Effort (est.) |
|---|---|---|
| 0 | Owner approves; tag current heads (`archive/phase1-427b137`, `archive/phase2-f174a06`, `archive/phase3-9d09629`, `archive/phase4-c13f022`). Create `integration/dealerflow` from `charming-ritchie`. | 0.5 d |
| 1 | Merge Phase 2 (resolve `deps.py`, `app.py`, `models.py`, `config.py`, `jobs.py`, `worker.py`; keep P1 security fixes; apply netguard to website research). Gate: P1 136 + P2 146 tests pass. | 1–2 d |
| 2 | Merge Phase 4: regenerate its core migration as `0003` against P2's `clients`; drop interim 2/3 tables; rewire reports/portal to P2 inventory. Gate: all three suites pass. | 3–5 d |
| 3 | Port Phase 3: services (`responder`, `inbox`, `automation_policy`, `appointments`, `summaries`, `escalation`, `evaluation`, `evals/`), models renamed to `client_id`, migration `0004`, dealer UI tabs. Add `verified_at` + conflict fields to P2 vehicles. Gate: ported Phase 3 tests + 46-case evaluation pass. | 5–8 d |
| 4 | Port Phase 3.5 safety layer: send gate + ledger + uncertain reconciliation for all channels, DB rate counters, pilot allowlist/console, MFA + reset + server-side sessions, audit HMAC, config validation. Migration `0005`. Gate: ported 3.5 tests + smoke script pass. | 4–6 d |
| 5 | Cross-cutting: one linter (ruff) in CI, fix the P1 retry bug and P4 optional-extra test, Docker build verified, reproducibility run from a clean clone. | 1–2 d |
| 6 | Owner review; only then retire `app/` (branch and tag kept) and open the integration branch as the PR into `main`. | 0.5 d |
| | **Total** | **≈15–24 engineering days** (estimate; largest uncertainty in steps 2–4) |

## 8. Issues found in the other branches (to fix during integration, not fixed here)
- P1: re-approving a retried send reuses a finished job key, so the message never sends (reproduced by the analysis); Gmail send retries a POST that may already be delivered; daily cap counts from UTC midnight; cap check can race with >1 worker; in-memory login limiter; SSRF DNS-rebinding gap; `/inbox/{id}/reclassify` lacks 404/validation.
- P2: missing six later Phase 1 commits (session revocation, SSRF fix, audit hardening, TxDMV, retention, livecheck); no human-verified timestamp; staff access to clients not scoped; dealer users rely on a staff-role backstop.
- P4: SEO crawler has no SSRF guard; optional-extra test failure; in-memory rate limits keyed on client host; automation gates guard workers that don't exist; Docker never built.
