# Pilot readiness — one dealership, sandbox

_Phase 3.5, 2026-10-18. Branch `claude/magical-darwin-nncp64`._

## Verdict
**Ready for a controlled one-dealership pilot in SANDBOX mode** (staff use the portal on synthetic or staff-entered
data; nothing leaves the platform). **Not yet ready for live customer messaging:** no provider has been tested with a
real account, Meta/Twilio approvals don't exist, and the open `dealerflow` foundation conflict must be decided first.
A tightly-scoped *live test to internal recipients only* is technically supported (pilot allowlist) but requires your
explicit approval, credentials, and the steps in `docs/architecture/integration-setup.md`.

## What the pilot configuration guarantees (each backed by tests)
| Guarantee | Mechanism | Tests |
|---|---|---|
| All outgoing messages are simulated or drafts until explicitly authorized | registry returns sandbox adapters unless `LIVE_INTEGRATIONS_ENABLED` + credentials + recorded live authorization; Mode A (draft-only) default; website chat also needs authorization | `test_pilot_mode.py::test_no_live_authorization_means_simulated_even_with_env_on`, `::test_website_chat_requires_authorization_and_allowlist`, `test_automation_modes.py` |
| Approved live tests reach only designated internal recipients | `PILOT_MODE=true` (default) + `pilot_recipients` allowlist enforced in `send_gate` for every channel | `test_pilot_mode.py::test_live_send_blocked_unless_recipient_allowlisted`, `::test_live_send_to_allowlisted_internal_recipient` |
| Visible global emergency stop prevents new sends | `platform.emergency_stop_all` checked in `send_gate` (customer, outreach, notification email) + outreach agent halted; red banner on every page | `test_pilot_mode.py::test_emergency_stop_blocks_allowlisted_live_send`, `test_worker_resilience.py::test_global_pause_and_emergency_stop_survive_restart`, smoke run |
| Every attempted action is logged with its result | `send_attempts` ledger (BLOCKED / IN_FLIGHT / SENT / SIMULATED / FAILED / UNCERTAIN) + HMAC-sealed append-only audit log | ledger assertions across tests; `test_security_hardening.py::test_audit_log_is_append_only_and_tamper_evident` |
| Operators can inspect drafts, failed jobs, escalations, worker health | **Admin → Pilot console** (`/admin/pilot`) | `test_pilot_mode.py::test_pilot_console_controls`, smoke run |
| Documented, one-click return to sandbox | Pilot console → *Return to sandbox* (revokes all live authorizations) + env switch | `test_pilot_mode.py::test_pilot_console_controls` |

## Security & reliability findings (audit) and status
| ID | Finding (before Phase 3.5) | Severity | Status | Test |
|---|---|---|---|---|
| W1 | Customer send changed status and called the provider in one transaction → crash after provider acceptance could resend | High | **Fixed**: atomic claim, IN_FLIGHT ledger committed before call, crash → UNCERTAIN, operator reconciliation | `test_worker_resilience.py::test_crash_after_provider_call_never_resends` |
| W2 | Outreach message could be sent twice by concurrent jobs/workers | High | **Fixed**: atomic APPROVED→SENDING claim | `::test_two_workers_cannot_send_the_same_outreach_message` |
| W3/W4 | Send/auto-reply limits and budgets computed by counting rows (race across workers) | Medium | **Fixed**: DB counters with conditional UPDATE | `::test_distributed_counter_never_exceeds_limit_under_concurrency`, `test_outreach_agent.py::test_rate_and_budget_limits_defer` |
| W5/W6 | No stop that covered staff messages and notification email | Medium | **Fixed**: platform emergency stop in central gate | pilot tests |
| W7 | Timeouts treated as retryable (possible duplicates) | Medium | **Fixed**: `uncertain` results never auto-retried (Meta, Twilio, SMTP, sandbox) | `::test_provider_timeout_marks_uncertain_without_retry`, `test_role_matrix.py::test_uncertain_notification_reconciliation` |
| W8 | Outreach emergency stop permanently BLOCKED approved messages | Low | **Fixed**: held (APPROVED) until reset | `test_outreach_agent.py::test_emergency_stop` |
| W9 | Worker died on DB outage? | — | Verified OK (loop survives), now tested | `::test_worker_survives_database_outage` |
| A1 | Safety checker ran on the draft, not on the final outgoing text; inventory could change between draft and send | High | **Fixed**: AI/system messages re-validated at send against current facts (fact snapshot); staff text checked with audited override | `test_accuracy_hardening.py` (4 tests) |
| A2 | Mode B could auto-send clarifying questions; ambiguous vehicle not escalated | Low | **Fixed** | `::test_ambiguous_vehicle_escalates` |
| A3 | Customer cancellation only escalated; appointment state unchanged | Low | **Fixed**: RESCHEDULE_REQUESTED + event; staff decide | `::test_customer_cancellation_flags_appointment_for_staff`, `::test_spanish_cancellation_and_reschedule` |
| A4 | Mileage stated from stale/unverified records | Low | **Fixed**: same freshness rule as price/availability; default freshness 24h | `::test_stale_mileage_not_stated` |
| P1 | Website chat replies reached real visitors without any live authorization | High | **Fixed**: chat needs `customer_messaging.WEBSITE_CHAT` + allowlist in pilot | `test_pilot_mode.py::test_website_chat_requires_authorization_and_allowlist` |
| S1/S2 | Login and chat throttles in-process (reset on restart, not shared) | Medium | **Fixed**: DB counters | `test_security_hardening.py::test_login_throttle_is_shared_across_app_instances`, `test_portal_security.py::test_website_chat_api_and_rate_limit` |
| S3 | Logout without CSRF | Low | **Fixed** | `::test_logout_requires_csrf_and_ends_session` |
| S4 | No startup config validation; unused `SECRET_KEY` | Medium | **Fixed**: fail-fast `validate_settings` (web + worker); key removed | `::test_config_validation_fails_closed` |
| S5 | Raw Meta webhook payload stored unredacted customer text | Medium | **Fixed**: ids only | `::test_raw_meta_events_do_not_store_message_text` |
| S6 | Audit log mutable | Medium | **Fixed (partially)**: ORM append-only + per-row HMAC + id-gap detection; DB superuser can still delete newest rows undetected → ship logs off-box in production | `::test_audit_log_is_append_only_and_tamper_evident`, `test_migrations.py` |
| S7 | No password reset / MFA | High | **Fixed**: TOTP MFA (replay-protected, recovery codes), admin MFA required in production, reset/setup links, sessions invalidated | 7 tests in `test_security_hardening.py` |
| S8 | Sessions survived password change / deactivation | Medium | **Fixed** | `::test_password_reset_flow`, `::test_deactivation_ends_sessions` |
| S9 | Temporary passwords and ADF tokens in redirect URLs | Medium | **Fixed**: shown once in response body; no admin-chosen passwords | `::test_admin_new_member_gets_setup_link_not_password_in_url` |
| S10 | No CSP/HSTS; stack traces? | Low | **Fixed**: CSP, frame-ancestors none, no-store on authenticated pages, HSTS in production, generic 500 with reference id | `::test_security_headers_and_generic_errors` |
| S11 | Staff could approve listings | Low | **Fixed**: dealer admin only; full role matrix tested | `test_role_matrix.py` (24 tests) |
| S12 | Unbounded CSV/ADF uploads | Low | **Fixed**: 2 MB / 1 MB | — (simple guard) |

## Unresolved risks
1. **Two competing foundations** (`app/` here vs `dealerflow/` on `claude/dealership-inventory-publishing-iq0aqw`). Decision + port needed before production (plan in `docs/architecture/shared-stack.md`).
2. **No real-provider verification.** All provider behaviour (error codes, rate limits, webhook shapes) is from documentation and mocks.
3. **SQLite.** Fine for a single-host pilot; PostgreSQL is supported by the code but not tested in this environment.
4. **Rule-based language understanding.** Conservative by design (unknown → escalate), but unusual phrasing yields more staff work; no live-traffic evaluation yet.
5. **Audit tail deletion** by someone with direct DB access is not detectable in-database.
6. **Reminders depend on the worker running**; a long worker outage delays them (the recovery sweep re-schedules; late reminders are skipped if the time passed).
7. **Legal review** of outreach (CAN-SPAM, Texas ch. 321, TCPA/SB 140) and customer messaging consent has not been done by counsel.

## Required credentials (only for a later, approved live test)
See `docs/architecture/integration-setup.md`. Minimum for a pilot live test of staff replies via website chat: none from
third parties — only `LIVE_INTEGRATIONS_ENABLED=true`, a `customer_messaging.WEBSITE_CHAT` authorization for the pilot
dealership, and the tester's chat visitor id on the allowlist. Email alerts need SMTP credentials.

## Estimated operating costs (estimates — verify against current provider pricing before committing)
| Item | Estimate | Basis |
|---|---|---|
| Hosting (1 small VM: web + worker, SQLite or small managed Postgres) | ~$10–50 / month | typical VPS / managed DB entry tiers (not quoted) |
| Website chat, ADF intake, Marketplace assisted | $0 provider cost | first-party |
| Meta Messenger | $0 | Meta: "The Messenger Platform is free to use" (research) |
| Twilio SMS | one-time ~$4.50–$46 brand + $15 campaign vetting; ~$1.50–$10 / month campaign; per-message + carrier fees | research **[unverified current]** |
| Email (operator mailbox) | Google Workspace / M365 seat (~$7–$15 / user / month, not quoted) | — |
| Google Places discovery | ~$0.035 per Text Search call after free tier | research (Enterprise SKU) |
| Claude rephrasing (optional, off by default) | ~0.5¢ per reply at ~600 input + 150 output tokens on claude-opus-5-5 ($4/$20 per M) | list price; app records estimates in `cost_entries` |
| Zoom | paid plan likely required for S2S apps (~$15 / month, not quoted) | — |
The app records provider usage estimates in `cost_entries` (outreach dashboard). **No paid service was used in this phase.**

## Step-by-step SANDBOX pilot procedure
1. **Install & migrate**: `uv sync`; `cp .env.example .env`; set `ENCRYPTION_KEY`; keep `LIVE_INTEGRATIONS_ENABLED=false`, `PILOT_MODE=true`; `uv run alembic upgrade head`.
2. **Create the operator account**: `uv run python -m scripts.seed_demo` (demo data) or create the pilot dealership in Admin → *Add client*; add the dealership's staff (Admin → client → Team: each gets a one-time setup link).
3. **Enable MFA** for every platform admin (Account → Two-step verification). It is mandatory when `APP_ENV=production`.
4. **Start processes separately**: `uv run uvicorn app.main:app` and `uv run python -m app.worker`. Check `/healthz` and `/healthz/worker` (200), and the Pilot console shows the worker alive.
5. **Dealer setup** (dealer admin): Business info & hours → approve; Inventory → enter/verify vehicles (price/availability count as verified for 24h); FAQs & templates → approve.
6. **Run the evaluation** (Portal → Automation → Run evaluation) — must PASS. Keep **Mode A (draft-only)** for the pilot.
7. **Daily pilot loop**: staff log real Marketplace messages via *Log a Facebook Marketplace message* (human-assisted) or use the chat test page with synthetic questions; review drafts; "send" (recorded as SIMULATED, or "Record as sent" after pasting into Marketplace); confirm appointments; resolve escalations.
8. **Operator checks** (Pilot console, daily): drafts awaiting staff, open escalations, failed jobs, deliveries needing reconciliation, send ledger (should contain **no live SENT rows**), audit integrity OK.
9. **Drills** (weekly): press *EMERGENCY STOP ALL SENDS*, confirm banner + a blocked attempt in the ledger, lift with a reason; restart the worker and confirm health returns.
10. **Smoke test** after every deploy: `SMOKE_BASE_URL=… uv run python -m scripts.smoke_e2e` (24 checks; exits non-zero on failure).

## Switching back to sandbox (any time)
1. Pilot console → **EMERGENCY STOP ALL SENDS** (immediate, all processes).
2. Pilot console → **Return to sandbox** (revokes every live authorization → all adapters become sandbox).
3. Set `LIVE_INTEGRATIONS_ENABLED=false`, restart web and worker.
4. Reconcile any UNCERTAIN deliveries in the Pilot console, then lift the emergency stop with a reason.
