# MyBot unification: porting plan

**Goal:** one application (`dealerflow` package, renamed later if you want "MyBot" in code) that contains Phases 1–4,
keeping the strongest tested control for every safety property. Branch: `claude/mybot-unification`.
Source branches stay untouched; each increment below is one or more small commits with its own tests.

## Rules for every increment

1. **No control may get weaker.** Each ported area adds regression tests to `tests/test_unification_controls.py`
   (or an area-specific file). Any test that guards a control must fail when that control is disabled (checked by
   temporarily disabling it, as was done for the emergency stop in increment 1).
2. **Every provider call goes through `audit.track_integration`.** That's where the emergency stop, disabled
   providers and failure logging live. Ported code that calls a provider directly is rewritten.
3. **Every outbound user-supplied URL goes through `services.netguard.assert_public_url`**, with redirects re-checked.
4. **One model per table.** Ported code adapts to the existing table; it never defines a second class with the same name.
5. **Migrations are linear on the P2 chain** (`0001` → `0002` → …). Revisions from other branches are re-expressed, not copied.
6. The full suite passes on SQLite and Postgres before each push.

## Increments

| # | Increment | Source | Key work | Exit criteria | Status |
|---|---|---|---|---|---|
| **1** | Phase 2 + hardened foundation | P2 + P1.5 | Merge; resolve `deps.py` (dealer confinement + password-bound sessions); gate AI listing calls; single SSRF guard; CSRF on all unsafe methods | 203 tests pass; mutation check on the emergency stop | **Done** (`228aacd`, `0f995d4`, `8eea527`) |
| **2** | Cross-cutting security from P3 | P3 | (a) server-side sessions, keeping password binding; (b) TOTP MFA + recovery codes, required for admins in production; (c) password reset and setup links; (d) DB-backed login throttle with constant-time dummy hash; (e) HMAC-sealed append-only audit + integrity check; (f) Fernet encryption key from the secrets dir, for per-client credentials | Each sub-part has its own tests; existing auth tests still pass | Planned |
| **3** | Phase 4 back office | P4 | One `Client` model: P2 columns plus P4 `prospect_id`, status, languages, hours. Drop P4 `users.client_id` duplicate and role `client` (use P2 `client_role`). Renumber P4 migration to `0003` without the `clients` create. **Don't port interim tables.** Port finance, SEO, tickets, onboarding, reports, Stripe webhooks, portal sections. Wrap the SEO crawler in netguard with manual redirects. Restore production config checks (http base URL with sending enabled = error). Bound the API rate limiter's memory. Port P4's CSP-friendly `data-confirm` JS and drop `script-src 'unsafe-inline'` | P4 tests rewritten against unified models pass; P2 isolation tests pass; SEO crawler SSRF tests | Planned |
| **4** | Send ledger and pilot controls | P3 | `SendAttempt` ledger inside the send path (outreach email now; customer messages in #6); UNCERTAIN state with an operator reconcile page instead of "failed: outcome unknown"; pilot allowlist (`PilotRecipient`) replacing `DF_TEST_INBOXES`; per-scope `LiveAuthorization` with statement replacing the single `live_authorized` flag; DB `RateCounter` for send caps and budget reservation | Existing outreach adversarial tests pass unchanged; new ledger, reconcile and rate tests | Planned |
| **5** | Customer data model | P3 | `customer_contacts` (consent), identities, leads, per-client-and-channel suppression, `conversations`, `messages`, `inbound_events`, `escalations`, appointments, all on `clients.id`; P2 `vehicles` gains `verified_at` + `approved_features` | Isolation tests for every new table | Planned |
| **6** | Response engine and channels | P3 | Responder (analysis, fact sheet, compose, `validate_output`), automation modes A/B/C gated by evaluation, 46-case eval harness; channels: website chat (fix visitor-id-only poll auth), Messenger, Twilio SMS (consent, STOP, 21610), ADF intake. All channel sends through gate + ledger; LLM rephrase through `track_integration` | Eval harness passes at 1.0; STOP and consent tests; emergency stop blocks every channel | Planned |
| **7** | Appointments | P3 + P1.5 | Test-drive lifecycle on P1.5 meeting infrastructure: atomic status claim, deterministic calendar IDs, Zoom recovery, no synchronous calendar create in requests | Double-submit and lost-response tests | Planned |
| **8** | Consolidation | all | One dealer workspace route prefix; one docs index; delete dead code (P3 duplicate outreach CRM); Docker build and run; Postgres suite; update audit docs | Clean tree; full suite on both databases; container run | Planned |

## Known collisions (and how each is resolved)

| Collision | Resolution |
|---|---|
| `clients` table defined by P2 and P4 | Single model; P4 columns added by migration `0003` |
| `users.client_id` + FK `fk_users_client_id` added by both P2 and P4 | Keep P2's; P4's role `client` → P2 `client_role` |
| Revision `'0002'` in both P2 and P4 | P4 re-expressed as `0003` |
| P4 interim `vehicles`, `listings`, `inventory_imports`, `publish_attempts` | Not ported; P4 reports read P2 tables |
| P4 interim `conversations`, `conversation_messages`, `appointments` | Not ported; replaced by P3 models in #5 |
| P3 outreach `Prospect`, `Campaign`, `Enrollment`, `OutreachMessage`, `Suppression`, `CostEntry` | Not ported (P1.5 is authoritative); compliance ideas reviewed individually |
| P3 `Dealership` + `Membership` (many-to-many) vs single `users.client_id` | Single client per user for now; **your decision** if dealer groups need one login across lots |
| Two job runners | Keep `dealerflow/jobs.py`; P3 handlers re-registered on it |
| Two `/portal` designs (P3 dealer ops, P4 billing/reports) + P2 `/dealers` | One workspace; **your decision** on the URL prefix |

## Decisions needed from you

1. **Dealer groups:** must one dealership user be able to switch between several lots (P3 many-to-many), or is one user = one dealership enough?
2. **Workspace URL:** keep P2's `/dealers/{id}` or P3/P4's `/portal`? (This is cosmetic; the plan works either way.)
3. **Package name:** keep `dealerflow` internally (no churn) or rename to `mybot` in increment 8?
4. **Auto-sent client emails** (P4 monthly reports): allowed without per-message approval, or held for review like outreach?
5. Confirm the order above. Security (#2) before features (#3–#7) is my recommendation.
