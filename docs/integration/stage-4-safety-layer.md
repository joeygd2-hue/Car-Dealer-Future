# Stage 4 — port the Phase 3.5 safety layer across the unified app

_Written before the change. Source: Phase 3.5 work on `claude/magical-darwin-nncp64` (`98fa3d3`). Stage 3 already
brought the send gate, the send ledger, DB rate counters and the pilot-mode switch for **customer messages and
dealer notification email**. This stage extends them to every other send path and ports the remaining controls._

## What already exists in dealerflow (kept, not replaced)
| Control | dealerflow today | Decision |
|---|---|---|
| Platform emergency stop | Phase 1 `agent_state.status = emergency_stopped` halts every provider call (`track_integration`) | **Keep** as the single emergency stop; the send gate already reads it. |
| Outreach live gate | `DF_OUTREACH_SENDING_ENABLED` + admin live authorization (Agent page) + manager approval | Keep; add the pilot allowlist on top. |
| Outreach duplicate protection | message committed `sending` before the provider call; a re-run never resends | Keep; also record every attempt in the send ledger. |
| Secrets | environment / secret files only, nothing in the DB | **Keep the env-only model.** Phase 3.5's Fernet-encrypted DB credentials are *not* ported; per-client channel credentials are referenced by env-var name (`config.secret_env`), the pattern Phase 2 already uses for inventory feeds. |
| Session revocation | signed cookie + password-version fingerprint | Replaced by server-side sessions (below), which also revoke on password change. |
| Audit log | `audit_log` rows, secret-key redaction | Add per-row HMAC seal + append-only guard (below). |

## Changes
| Area | Change | Tables / migration `0006` |
|---|---|---|
| Pilot allowlist + console | `pilot_recipients` (email / phone / chat visitor / Messenger PSID). While `DF_PILOT_MODE=true` (default) a **live** send of any kind — customer reply, dealer notification, client report email, outreach email — goes only to an allowlisted internal recipient; everything else is blocked and logged. Admin console `/admin/pilot`: switches, allowlist, ledger, UNCERTAIN items, "return everything to sandbox". `DF_TEST_INBOXES` entries count as allowlisted emails. | new `pilot_recipients` |
| Send ledger everywhere | Phase 1 outreach email and Phase 4 client email (reports, lead summaries) record `send_attempts` and pass the same gate. | uses `send_attempts` |
| Outreach retry bug (Phase 1) | Re-approving a failed message reused the finished job key, so it was never sent. Each approval now gets its own job key. | — |
| Server-side sessions | `user_sessions` (random id in the signed cookie, SHA-256 stored); logout and password change end sessions; admins can end all of a user's sessions; idle + absolute expiry. | new `user_sessions` |
| MFA | TOTP (RFC 6238, stdlib) with replay protection and one-time recovery codes; `/account/security` to enrol; login asks for the code; `DF_REQUIRE_ADMIN_MFA` (default **true in production**) blocks admins without MFA from admin pages until enrolled. | `users`: + `mfa_secret`, `mfa_enabled`, `mfa_last_counter`, `mfa_recovery_hashes` |
| Password reset / setup links | Hashed, single-use, 1-hour tokens; using one ends every session of that user; emailed through the configured provider (mock in dev). | new `password_reset_tokens` |
| DB-backed rate limits | Login throttle and the `/api/` + webhook limiter move from per-process memory to `rate_counters` (multi-worker safe). | uses `rate_counters` |
| Audit integrity | Every `audit_log` row is sealed with an HMAC (key derived from `DF_SECRET_KEY`); ORM refuses UPDATE/DELETE of audit rows; `python -m dealerflow audit-verify` and the console report rows whose seal does not match. Existing rows are sealed by the migration. | `audit_log`: + `entry_hmac` |
| Config fail-fast | Production refuses to start without MFA enforcement for admins, with pilot mode off but no live switches understood, etc. (exact list in `config.production_problems`). | — |
| Live customer adapters | Messenger Send API and Twilio SMS adapters (from Phase 3), credentials from env vars named on the connection, used only when `DF_LIVE_CUSTOMER_MESSAGING_ENABLED` + connection `connected` + pilot allowlist. Tested against mocked HTTP only. | — |

## Data-loss risk
None. All schema changes are additive (new tables, new nullable/defaulted columns). Sealing existing audit rows
only fills the new `entry_hmac` column. Server-side sessions log every user out once (expected; announced in the
build status).

## Known limitation carried forward
Audit "gap" detection cannot distinguish a deleted row from an id skipped by a rolled-back transaction
(PostgreSQL sequences skip values on rollback), so gaps are reported as *informational*; tampering with a row's
content is detected by its seal.

## Gate
Ported Phase 3.5 tests (pilot mode, security hardening, role matrix, migrations, send-race tests) pass, all earlier
tests pass, on SQLite and PostgreSQL.
