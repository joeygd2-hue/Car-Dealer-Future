# Stage 3 — port Phase 3 (customer response) into dealerflow

_Written before the port. Source: `app/` on `claude/magical-darwin-nncp64` at `98fa3d3` (local tag
`archive/phase3-98fa3d3`). Target: `claude/integration-dealerflow` after stage 2._

## What is ported
| Phase 3 part | Lands in | Notes |
|---|---|---|
| Response engine: language detection, rule-based EN/ES analysis, grounded composer, output safety checker | `dealerflow/services/responder/` | Pure code, copied with import changes only. Engine version unchanged (`1.0.0`). |
| Fact sheet (verified, fresh, non-conflicting facts only) | `services/responder/facts.py` | Re-pointed at Phase 2 `vehicles` and Phase 2/4 `clients` (see mapping). |
| Inbox pipeline (ingest/dedupe → analyze → draft → decide → send/escalate → summarize) | `services/customer/processing.py` | |
| Outbound send with atomic claim, send-time re-validation, ledger, UNCERTAIN reconciliation | `services/customer/outbound.py`, `services/customer/send_gate.py` | The send gate and its ledger are ported here because Phase 3's send path cannot work without them. Stage 4 extends the same gate to the other send paths. |
| Automation modes A/B/C, pauses, takeover, DB rate counters | `services/customer/policy.py`, `controls.py`, `rate_limits.py` | B/C additionally require Phase 4's `inquiry_auto_response` automation (onboarding-gated); **both** gates must pass. |
| Escalations, summaries, staff notifications + digest | `services/customer/{escalation,summaries,notify}.py` | Notification email goes through Phase 4's `client_email` (simulated unless that client's `report_email` connection is authorized). |
| Test-drive appointments (never "confirmed" until staff confirm) | `services/customer/appointments.py` | |
| ADF/XML lead intake (defusedxml) | `services/customer/adf.py` | Adds dependency `defusedxml`. |
| Evaluation harness + 46 synthetic EN/ES cases | `services/customer/evaluation.py`, `dealerflow/evals/` | Runs against an isolated in-memory DB. |
| Customer channels: first-party website chat, ADF, Marketplace-assisted (manual), Messenger/Twilio **inbound** webhooks | `integrations/customer_channels.py`, `web/routes_customer_public.py` | **Every outbound customer message is SANDBOX in this stage.** Live Messenger/Twilio adapters and encrypted per-client credentials come in stage 4. Website chat replies are only "live" for a client whose `customer_messaging/website_chat` connection is `connected` **and** `DF_LIVE_CUSTOMER_MESSAGING_ENABLED=true` (default false). |
| Dealer UI (inbox, conversation, appointments, automation, content, notifications) | `web/routes_inbox.py`, `templates/inbox_*.html` | Tabs inside Phase 2's `/dealers/{client_id}` workspace, using its tenant-scoped `ClientContext`. |

Not ported: Phase 3's `app/` outreach agent, prospect tables, its own auth/session/user/membership model,
its inventory/listing stand-ins and its job queue — dealerflow's Phase 1/2 versions are kept (decision doc §2).

## Tables
New (all tenant-owned tables use Phase 2's `TenantMixin`, so the tenant session filter and flush guard apply):
`customer_contacts`, `customer_identities`, `customer_leads`, `contact_suppressions`, `customer_conversations`,
`customer_messages`, `customer_inbound_events`, `response_drafts`, `escalations`, `test_drive_appointments`,
`test_drive_appointment_events`, `response_automation_settings`, `mode_authorizations`,
`approved_response_templates`, `faqs`, `evaluation_runs`, `dealer_notification_preferences`,
`dealer_notifications`, `send_attempts`, `rate_counters`.

Altered (additive only):
- `vehicles`: + `data_conflict_note` (forces escalation when sources disagree).
- `clients`: + `business_info_approved`. Phase 4's unused `business_hours` JSON becomes the structured weekly
  hours `{"0": ["09:00","19:00"], …, "6": null}` the engine needs.
- `integration_connections`: + `paused`, `external_account_id`, `config`.

Name conflicts resolved by naming, not by dropping: Phase 4's interim `conversations`,
`conversation_messages`, `appointments` stay in the schema **unchanged**; the migration **copies** their rows
into the new tables (`legacy_*_id` columns keep the link) and the code stops using them. Phase 4's
`automation_settings` (per-feature gates) keeps its name; Phase 3's mode settings become
`response_automation_settings`.

## Field mapping
- Dealership → `clients` (name, timezone, `phone`, `address_line`/`city`/`state`/`postal_code`, `business_hours`,
  `business_info_approved`).
- Phase 3 `Vehicle.verified_at` → Phase 2 `last_seen_at` (set by every feed/file sync that sees the vehicle and by
  staff "confirm availability"). Fresh = seen within the dealership's freshness window (24 h default) **and** not
  `stale` **and** `validation_status != "invalid"` **and** no `data_conflict_note`.
- Status: `available/pending/sold` → `AVAILABLE/PENDING/SOLD`; `removed` → unknown (escalates).
- Approved features → Phase 2 `features` (the dealer's own list; Phase 2 already treats it as the verified list).
- Phase 3 roles → Phase 2 dealer roles via a new `respond` permission (staff rep+, `dealer_admin`,
  `dealer_reviewer`) and the existing `manage` permission for automation/content settings. Raising a mode above
  draft-only is a **dealership** decision: only a `dealer_admin` user of that client can record the authorization.

## Jobs
`customer.process_message`, `customer.send`, `customer.notify_email`, `customer.appointment_reminder`,
`customer.daily_digests` (hourly check, once per local day per client), on dealerflow's job queue
(atomic claim, `DeferJob`, failure hooks). The "another worker may still be sending" window uses the
queue's stale-lock interval (15 min).

## Auth rules added
Dealer workspace routes use `authorize_client` (404 for another client's ids). Public endpoints: chat widget
API keyed by an unguessable per-client key, Meta/Twilio webhooks with signature verification (refused when
the secret is not configured), ADF intake with a per-connection token.

## Data-loss risk
None by design: no table or column is dropped or rewritten; interim rows are copied, originals untouched.
Downgrade of `0005` drops only the new tables/columns. Dropping the interim tables is **deferred to an
explicit owner decision**.

## Gate
Phase 3 tests ported for this scope pass, the 46-case evaluation passes its thresholds, and every earlier
test still passes.

## Result
Commits `46f0eef` (services, migration 0005), `05f6217` (UI + public endpoints), a fix restoring Phase 1's
`templates/inbox.html` that 3b had overwritten (caught by `test_web`; now byte-identical to Phase 1), the ported
tests, and a fix found by them: a permanent email-provider rejection (4xx) was recorded as UNCERTAIN instead of
FAILED (`services/customer/notify.py`).

- Evaluation: **46/46 synthetic EN/ES cases, every metric 1.0** (thresholds: factual, escalation recall and
  opt-out 1.0; others 0.95).
- Ported Phase 3 tests: **94 pass** (55 engine/language/pipeline/accuracy/ADF, 39 automation modes,
  appointments/notifications, outbound/webhooks, portal security, send-race/worker resilience), plus 3 new
  (migration copy, 2 smoke). Assertion counts are unchanged except where noted below.
- Full suite: **SQLite 355 passed; PostgreSQL 16 354 passed, 1 skipped** (the SQLite-only backup test).

Not ported, because the feature does not exist in this build (listed as known limitations):
- Per-dealership test-drive calendar (calendar failure test; event-id and busy-slot checks in the confirmation
  test). Appointments report availability as NO_CALENDAR and are confirmed by staff.
- Messenger Send API / Twilio SMS send adapters (adapter tests). Those channels receive (signed webhooks) but
  reply through the sandbox; the live adapters are stage 4 scope.
- Outreach-agent and old-worker internals from `test_worker_resilience.py` (dealerflow keeps Phase 1's queue and
  agent, which have their own tests).
