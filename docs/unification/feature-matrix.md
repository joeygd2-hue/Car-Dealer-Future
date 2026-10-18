# MyBot unification: feature matrix

Compared 2026-10-18 at these commits:

| Code | Branch | Commit | Shape |
|---|---|---|---|
| **P1.5** | `claude/charming-ritchie-358e1g` | `427b137` | `dealerflow` Phase 1 + launch-readiness hardening (136 tests) |
| **P2** | `claude/dealership-inventory-publishing-iq0aqw` | `f174a06` | `dealerflow` from `7f0db48` (pre-hardening) + inventory, listings, publishing, tenancy (183 tests incl. Phase 1) |
| **P3** | `claude/magical-darwin-nncp64` | `e2d3030` | **separate `app/` package**: customer-response engine, portal, own outreach agent (129 test functions; not runnable in this environment) |
| **P4** | `claude/blissful-davinci-82py6w` | `c13f022` | `dealerflow` merged up to `5f72e17` (pre-hardening) + client back office, finance, SEO, portal; interim P2/P3 tables (needs `beautifulsoup4`, `email-validator`; not run) |
| **U** | `claude/mybot-unification` | this branch | P2 + P1.5 merged, plus fixes (203 tests) |

**Authoritative** = the implementation the unified app keeps. **Port** = take code or ideas from another branch into it.
"✔" = present and tested; "◐" = partial or weaker; "✘" = absent.

## 1. Inventory and vehicle records

| Capability | P1.5 | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Vehicle model (per client, VIN unique) | ✘ | ✔ `vehicles`, photos, change history | ◐ `Vehicle` (verified_at, approved_features, price_cents) | ◐ interim `vehicles` | **P2** | Port P3's `verified_at` / `approved_features` semantics as columns on P2 `vehicles` (needed for the response engine's fact sheet) |
| Inventory import (CSV, feeds, validation, stale detection) | ✘ | ✔ sources, import runs, raw records, feed sync, SSRF-guarded | ✘ | ◐ CSV only | **P2** | |
| Listings (EN/ES, revisions, guardrails, review, AI + template generators) | ✘ | ✔ | ◐ `ListingDraft` | ◐ interim | **P2** | AI calls now gated (U) |
| Publishing (Marketplace manual, Meta catalog, idempotent operations) | ✘ | ✔ | ✘ | ◐ manual only | **P2** | |

## 2. CRM and prospect management (selling to dealerships)

| Capability | P1.5 | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Prospects, evidence, dedup, scoring, pipeline, tasks | ✔ | ✔ (inherited, pre-hardening) | ◐ separate `Prospect`/`ProspectFact` | ✔ (inherited) | **P1.5** | P3 outreach CRM not ported (duplicate) |
| TxDMV import profile | ✔ | ✘ | ✘ | ✘ | **P1.5** | |
| Won prospect → client conversion | ✘ | ✘ | ✘ | ✔ `create_client` (stops enrollments, suppresses as existing client) | **P4** | Port onto the unified `clients` table |

## 3. Authentication, roles, sessions, tenant isolation

| Capability | P1.5 | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Password hashing | PBKDF2-SHA256 390k | same | scrypt (N=2^14) | same as P1.5 | **P1.5** | Both acceptable; no migration needed |
| Sessions | Signed cookie + expiry + password-bound (`pv`) | signed cookie | **Server-side `UserSession` rows** (hashed token, per-session CSRF, revocable) | signed cookie | **P3 model + P1.5 password binding** | Port server-side sessions (fixes audit S4: no server revocation) |
| MFA | ✘ | ✘ | ✔ TOTP + recovery codes, replay guard, required for admins in production | ✘ | **P3** | Port (fixes audit S2) |
| Password reset / setup links | ✘ | ✘ | ✔ hashed single-use tokens | ✘ | **P3** | Port |
| Login throttling | in-memory per process | same | **DB counters** per IP and account | same | **P3** | Port, plus constant-time dummy hash (P3 weakness 7) |
| CSRF | session token; all unsafe methods (U) | POST only | per-session token, every POST | POST only | **U** | |
| Staff roles | viewer < rep < manager < admin | same | PLATFORM_ADMIN | client < viewer < … < admin | **P1.5 staff roles** | |
| Tenant users | ✘ | ✔ `users.client_id` + `client_role` (dealer_admin / reviewer / viewer), `ClientContext`, isolation tests | ✔ `Membership` (many-to-many) DEALER_ADMIN / STAFF; 404 on foreign ids | ✔ role `client` + `users.client_id`, `scope()` / `owned_or_404()` | **P2** | P4 and P2 both add `users.client_id` with the same FK name: unify on P2's. Many-to-many dealer groups (P3): **decision needed** |
| Tenant root table | ✘ | ✔ `clients` | `dealerships` | ✔ `clients` (**same name, different columns**) | **P2 + P4 columns merged** | One `Client` model |

## 4. Messaging, inbox, customer response

| Capability | P1.5 | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| B2B outreach email (CAN-SPAM, approval, replies, classifier) | ✔ | ✔ (pre-hardening) | ◐ duplicate outreach agent (has `ADV:` subject, Texas ch. 321 check) | ✔ inherited | **P1.5** | Check P3's Texas ch. 321 / `ADV:` claim against the statute before adopting it |
| Car-buyer conversations (web chat, Messenger, SMS, ADF leads) | ✘ | ✘ | ✔ | ◐ interim tables, simulated | **P3** | New domain; port onto `clients` + P2 `vehicles` |
| Grounded EN/ES response engine (fact sheet, banned claims, number check, drift check at send) | ✘ | ✘ | ✔ | ✘ | **P3** | Port with its 46-case evaluation harness |
| Automation modes A/B/C gated by evaluation | ✘ | ✘ | ✔ | ◐ automation flags only | **P3** | |
| Consent, STOP keywords, per-channel suppression, Twilio 21610 | ◐ email suppression only | ◐ | ✔ | ✘ | **P3 + P1.5 suppression** | |
| Client transactional email (monthly reports) | ✘ | ✘ | ✘ | ✔ (auto-send when enabled) | **P4** | Review: auto-send without approval |

## 5. Persistent agent and job runner

| Capability | P1.5 | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Job queue (idempotent keys, atomic claim, backoff, deferral, failure hooks, stale-lock recovery) | ✔ | ✔ | ✔ (leases, DEAD, `RetryLater`, recurring-task slots) | ✔ | **P1.5** | Equivalent strength; keep one runner |
| Per-worker heartbeat / stale worker detection | ◐ one heartbeat on `agent_state` | ◐ | ✔ `WorkerHeartbeat` table | ◐ | **P3 idea** | Port when running several workers |
| Outreach agent (bounded ticks, checkpoints, campaigns, budgets) | ✔ | ✔ (pre-hardening) | ◐ separate agent | ✔ inherited | **P1.5** | |

## 6. Emergency stop, campaign pause, delivery dedup

| Capability | P1.5 / U | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Global emergency stop | ✔ gates **every** provider call in `track_integration` (reads included); queue → draft | ◐ sends only | ✔ `platform.emergency_stop_all` in `send_gate` (sends only) | ◐ | **U** (covers P2 publish + AI) | |
| Send ledger (`SendAttempt`: BLOCKED / IN_FLIGHT / SENT / UNCERTAIN) | ◐ IntegrationLog + message state | ✘ | ✔ | ✘ | **P3** | Port into the gate for customer messages and outreach |
| Uncertain delivery | ✔ `sending` → failed "outcome unknown", never resent | ✘ | ✔ UNCERTAIN + operator reconcile UI | ✘ | **P3 UX on P1.5 rule** | |
| Pilot allowlist / per-scope live authorization | ◐ `DF_TEST_INBOXES`, single `live_authorized` | ✘ | ✔ `PilotRecipient`, `LiveAuthorization` per scope with statement | ✘ | **P3** | |
| Campaign pause holds queued sends | ✔ | ✘ | ✔ | ✘ | **P1.5** | |
| Distributed rate limits / budget reservation | ✘ in-process token bucket | ✘ | ✔ DB `RateCounter`, atomic budget reservation | ✘ (budgets only alert) | **P3** | Port (fixes the multi-worker limiter gap) |
| Duplicate email prevention | ✔ | ✔ (publish ops) | ✔ | ◐ | **P1.5 + P3** | |
| Duplicate booking prevention | ✔ deterministic calendar ids, Zoom lost-response recovery | n/a | ✘ booking race, no recovery | n/a | **P1.5** | Fix P3 test-drive flows when porting |
| Auth failure disables provider | ✔ | ✘ | ✘ | ✘ | **P1.5** | |

## 7. Audit, security controls, data retention

| Capability | P1.5 / U | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Audit log | ✔ actions + integration log (failures replayed after rollback) | ✔ | ✔ **append-only + per-row HMAC + integrity check** | ✔ + `record()` / `_scrub()` redaction | **P1.5 + P3 sealing + P4 scrub** | |
| SSRF guard | ✔ shared `netguard` (strictest) | ◐ | ✘ (no fetch surface) | ✘ **SEO crawler unguarded** | **U** | Must wrap the P4 crawler before merging it |
| Secrets | env or files (`DF_SECRETS_DIR`) | env | env + **Fernet-encrypted per-tenant credentials in DB** | env | **P1.5 + P3 Fernet** | Fernet for per-client credentials partly addresses audit S1 |
| Security headers / CSP | ✔ (`script-src 'unsafe-inline'`) | ✔ | ✔ | ✔ stricter (`script-src 'self'`, inline handlers removed) | **P4** | Port P4's `data-confirm` JS so CSP can drop `unsafe-inline` |
| Retention | ✔ message bodies, logs | ✘ | ◐ events and jobs | ✔ per-client export and delete | **P1.5 + P4** | |
| Sensitive-data redaction on inbound | ✘ | ✘ | ✔ SSN and card numbers | ◐ card rejection in finance | **P3** | |

## 8. Migrations, backup, restore

| Capability | P1.5 | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Alembic chain | `0001` | `0001` → `0002` (inventory) | own baseline (44 tables) | `0001` → `0002` (phase4) → `0003` (interim) | **P2 chain** | P4's `0002` must become `0003`; interim tables dropped; P3 tables re-expressed as new revisions |
| Postgres tested | ✔ full suite | ◐ untested | ✘ | ✘ | **P1.5** | Re-run on U |
| Backup / restore | ✔ (SQLite + Postgres verified) | ✘ | ✘ | ✘ | **P1.5** | |

## 9. Admin portal, reporting, integrations

| Capability | P1.5 / U | P2 | P3 | P4 | Authoritative | Notes |
|---|---|---|---|---|---|---|
| Staff UI (CRM, outreach, agent, settings, audit) | ✔ | ✔ | ◐ admin console | ✔ + clients, tickets, SEO, finance | **P1.5 + P4 sections** | |
| Dealer (client) workspace | ✘ | ✔ `/dealers/{id}` | ✔ `/portal` (inbox, inventory, appointments, automation) | ✔ `/portal` (invoices, reports, tickets) | **One workspace** built on P2's tenancy, with P3 and P4 sections added | Route prefix: **decision needed** (`/dealers` vs `/portal`) |
| Pilot console (stop, sandbox, allowlist, reconcile, worker health) | ◐ agent page | ✘ | ✔ | ✘ | **P3 on the P1.5 agent page** | |
| Finance, SEO, client reports, support tickets | ✘ | ✘ | ✘ | ✔ | **P4** | |
| Email / calendar / Zoom / calls providers | ✔ (mock, SMTP, Gmail, Google, Zoom S2S, Twilio calls) | ✔ inherited | ✔ SMTP, Google Calendar, Zoom, Twilio SMS, Messenger | ✔ inherited + Stripe webhooks | **P1.5 interfaces + P3 Messenger/SMS + P4 Stripe** | |

## 10. Tests, deployment, documentation

| Capability | P1.5 | P2 | P3 | P4 | Authoritative |
|---|---|---|---|---|---|
| Tests | 136 (adversarial, Postgres) | +47 | 129 functions + 46-case eval harness + e2e smoke script | ~142 (not runnable here) | **Union**: P3 tests rewritten against `dealerflow` |
| Docker / Compose | ✔ built and run, file secrets | ✔ | ✘ | ✔ | **P1.5** |
| Docs | audit, compliance, research | inventory docs | threat notes, pilot readiness, channel research | threat model, ops runbook, business docs | **Union** under one index |
