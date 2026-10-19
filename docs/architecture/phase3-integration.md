# Phase 3 integration (customer conversations)

Integration branch `claude/mybot-phase3-integration`, created from `claude/blissful-davinci-82py6w` at
`194d247` (Phases 1 + 2 + 4). Phase 3 source: `claude/magical-darwin-nncp64` at `98fa3d3` — a separate
`app/` package with its own platform (auth, jobs, tenancy, outreach agent). Phase 3's own
`docs/architecture/foundation-decision.md` recommends exactly this direction: keep `dealerflow` and port the
Phase 3 services into it.

## What is ported, and where it lands

| Phase 3 (`app/…`) | dealerflow | Notes |
|---|---|---|
| `services/responder/{analysis,language,safety,engine}.py` | `services/conversations/responder/` | Pure logic, ported with lowercase channel/status codes |
| `services/responder/facts.py` | same package | Reads Phase 2 `Vehicle` (see "Vehicle facts") and Phase 4 `Client` business details |
| `services/inbox/processing.py`, `outbound.py`, `adf.py` | `services/conversations/{inbox,outbound,adf}.py` | Uses dealerflow jobs (`enqueue(db, kind, key, payload)`), `track_integration` gate, `audit()` |
| `services/automation_policy.py`, `escalation.py`, `summaries.py`, `appointments.py`, `evaluation.py`, `rate_limits.py` | `services/conversations/…`, `services/rate_limits.py` | Auto-send additionally needs the Phase 4 onboarding gate `inquiry_auto_response` |
| `services/notifications.py` | `services/conversations/notify.py` | In-app staff notifications; new-lead, escalation and test-drive e-mails go through Phase 4 `client_email.send` (SIMULATED unless authorized). Preferences and the daily digest were not ported |
| `security/mfa.py` | `security_mfa.py` | Stdlib TOTP + single-use recovery codes, secrets Fernet-encrypted |
| `web/public.py` (chat API, chat page, ADF intake) | `web/routes_conversations_public.py` | Per-client public key; DB rate limits; CORS limited to the client's website |
| `web/portal.py` (inbox, conversation, appointments, content, automation, business info) | `web/routes_conversations.py` under `/dealers/{id}/…` | Phase 2 `dealer_ctx` permissions; new `converse` permission |
| `evals/synthetic_conversations.json` (46 cases) | `dealerflow/evals/` | Gate for modes B/C, run against an isolated in-memory DB |
| Phase 3 tests | `tests/test_conv_*.py`, `tests/test_security_hardening.py` | Rewritten against dealerflow fixtures |

## Deliberately not ported (documented as limitations)

| Phase 3 item | Reason |
|---|---|
| `app/` auth, sessions, users, memberships, jobs, audit, outreach agent, CRM | dealerflow already has stronger or equivalent versions (Phase 1/1.5/2). One user = one dealership (Phase 2 rule) |
| Meta Messenger and Twilio SMS adapters + webhooks | Code-only in Phase 3, never verified; need Meta App Review / A2P 10DLC. Not ported to avoid unverified live code; channels stay listed as "not connected" |
| Per-dealership Google Calendar for test drives | Phase 3 only had a sandbox calendar for this; availability is recorded as `no_calendar` and staff confirm manually |
| Pilot allowlist console, send-attempt ledger table | No live customer channel exists to pilot. The message row is the ledger (`sending` + `sending_started_at` committed before delivery; a stale `sending` becomes `uncertain`, never re-sent) |
| HMAC-sealed audit log | Not ported; listed as a remaining item |
| Phase 3 inventory form / listing drafts | Phase 2 owns inventory and listings |

## Data model decisions

* Every new table carries `client_id` (Phase 2 `TenantMixin`) and references tenant parents with composite
  `(client_id, id)` foreign keys, so the database refuses cross-dealership links.
* The interim Phase 4 tables `conversations`, `conversation_messages`, `appointments` are **converted in
  place** (migration `0005`), not dropped: rows are kept, `customer_name`/`customer_contact` move into
  `customer_contacts`, interim drafts (`pending_approval` outbound rows) move into `response_drafts`.
* Statuses and channel codes are lowercase (dealerflow convention), e.g. `website_chat`, `awaiting_staff`.
* Customer opt-outs live in `customer_suppressions` (per dealership, per channel). Phase 1 `suppressions` stays
  the agency's prospect-outreach list; the two audiences are different and are never merged.
* Phase 3 `automation_settings` (response modes) is named `response_automation_settings`; Phase 4's
  `automation_settings` (onboarding gates) is unchanged.

## Vehicle facts (Phase 3 `verified_at` → Phase 2)

Phase 2 has no `verified_at`; its `last_seen_at` is set when a feed/upload reports the vehicle and when staff
press "Confirm availability", and `stale` is set by the stale scan. The fact sheet treats a vehicle as fresh
when `stale` is false and `last_seen_at` is within the dealership's freshness window (default 24 h). Field
conflicts come from Phase 2 validation issues: an `error` issue makes the whole record untrusted; price and
mileage issues withhold only that field. Features come from the dealer-supplied `features` list.

## Architecture after the integration

One application (`dealerflow`), one database, one job queue, one login:

```
public website ──► /api/public/chat/{key}/messages ─┐        ADF/XML ──► /api/public/leads/adf/{key} ─┐
staff paste (Marketplace) ──► /dealers/{id}/marketplace/intake ──────────────────────────────────────┤
                                                                                                    ▼
                 services/conversations/inbox.ingest_inbound  (dedupe, contact + identity + lead, vehicle link)
                                                                                                    │ job conversation_process
                                                                                                    ▼
 responder (analysis → facts from Phase 2 inventory + Phase 4 client → compose → validate) → policy.decide
        │ draft_only (default)                                │ auto_send (every gate passed)
        ▼                                                     ▼
 dealer inbox: review / edit / approve ──► outbound.queue_outbound ──► job conversation_send
                                                         │ claim → stop checks → revalidate → commit → track_integration
                                                         ▼
                                     website chat: sent (live, authorized) | simulated (default) ; other channels: staff send
```

Escalations, summaries, staff notifications and test-drive requests hang off the same pipeline. Everything is
tenant-scoped by `client_id`.

## Data model (migration `0005`, MFA columns in `0006`)

| Table | Purpose |
|---|---|
| `customer_contacts` | A dealership's car buyer (name, e-mail, phone, language, source, opt-out time, purge time) |
| `customer_identities` | Channel identifier → contact (`web:<visitor>`, `adf:<email>`, `mp:<hash>`); unique per client+channel |
| `customer_leads` | Sales opportunity created with the conversation (source, status, vehicle) |
| `conversations` | One thread per client+channel+external id; status, assignment, takeover, opt-out, summary, escalation and appointment status, response-time timestamps |
| `conversation_messages` | Inbound/outbound/system messages with delivery status (`received`, `queued`, `sending`, `sent`, `simulated`, `manual_external`, `blocked`, `failed`, `uncertain`), dedupe key per client+channel+provider id, idempotency key |
| `response_drafts` | Engine output: body, language, confidence, intents, facts used + snapshot, unresolved questions, escalation reasons, decision + reasons, review fields |
| `conversation_escalations` | Open/resolved escalations with reasons and recommended action |
| `appointments`, `appointment_events` | Test-drive requests and their history (`pending_confirmation` until staff confirm) |
| `customer_suppressions` | Per-dealership opt-outs (kept through retention purges) |
| `response_automation_settings`, `response_mode_authorizations`, `response_evaluation_runs` | Mode A/B/C, limits, pause; the dealership's written authorization tied to a passing evaluation |
| `response_templates`, `dealer_faqs` | Dealer-approved wording (optionally auto-send) |
| `staff_notifications` | In-app and e-mail notifications with real delivery status |
| `rate_counters` | Fixed-window counters shared by every process (chat, ADF, login, API, MFA, auto-replies) |
| `clients` (+4 columns) | `business_info_approved_at/by`, `public_chat_key`, `adf_intake_key` |
| `users` (+5 columns, `0006`) | TOTP MFA: enabled flag, encrypted secret, pending secret, last counter, hashed recovery codes |
| `legacy_conversations`, `legacy_conversation_messages`, `legacy_appointments` | Read-only archive of the interim tables; not mapped by any model |

### Migration and conversion

`python -m dealerflow migrate` runs `0005` and `0006`. `0005`:
1. drops the interim tables' named indexes, renames the tables to `legacy_*` (on PostgreSQL also their primary-key
   indexes and sequences);
2. creates the Phase 3 tables and the new `clients` columns;
3. runs `dealerflow.legacy_conversations.convert`, which copies every interim row (ids preserved; contacts, leads and
   identities created; interim unsent drafts become `response_drafts`; escalated conversations get an open escalation)
   and moves PostgreSQL sequences past the copied ids.

The copy is idempotent (rows already present are skipped; an interrupted contact/lead is reused), so
`python -m dealerflow convert-legacy-conversations` can be run again safely. Downgrade to `0004` drops the Phase 3 tables
(data created after the upgrade is lost) and renames the legacy tables back. Verified on SQLite and PostgreSQL 16 with
seeded interim rows: upgrade → assertions → re-run conversion (no-op) → drift check → downgrade → upgrade
(`tests/test_conv_migration.py`, which runs on both databases).

**Removing the legacy tables** is deliberately not done. Plan: after the upgrade is deployed and the converted data has
been checked against the archive (counts per client, a sample of conversations), add migration `0007` that drops the
three `legacy_*` tables; take a backup first.

## Routes

| Path | Who | What |
|---|---|---|
| `GET /chat/{key}` | public | Hosted chat page / reference widget (`static/chat.js`, no inline script) |
| `POST /api/public/chat/{key}/messages` | public | Inquiry (visitor_id, text, optional name/email/phone/vehicle_stock); DB rate limits; CORS only for the dealership's website |
| `GET /api/public/chat/{key}/messages` | public | Widget poll: only replies with status `sent` |
| `POST /api/public/leads/adf/{key}` | public (secret key) | ADF/XML intake, 256 KB limit, defusedxml |
| `/dealers/{id}/inbox`, `/conversations/{cid}`, `/appointments`, `/responses` | dealership users + staff | Pages (view: all roles; reply/confirm: `converse`; settings: `manage`) |
| `POST /dealers/{id}/conversations/{cid}/send`, `…/drafts/{did}/reject`, `…/assign`, `…/takeover`, `…/vehicle`, `…/close`, `…/messages/{mid}/reconcile` | `converse` | Review/edit/approve, reject, assign, take over, link vehicle, close, reconcile uncertain delivery |
| `POST /dealers/{id}/escalations/{eid}/resolve`, `/marketplace/intake`, `/appointments/{aid}/{confirm,decline,cancel,completed,no_show}` | `converse` | |
| `POST /dealers/{id}/responses/{business,keys,faqs,templates,pause,limits,evaluate,mode}` | `manage` | Business details + approval, intake keys, approved content, automation settings |
| `GET/POST /api/v1/dealers/{id}/conversations…`, `/appointments…`, `POST …/conversations/simulated-inquiry` | same roles | JSON API (session or API key); the simulated-inquiry tool marks conversations SIMULATED |
| `/login/mfa`, `/account/security`, `/account/mfa/{start,confirm,disable}` | users | MFA |

Permissions (`tenancy.PERMISSIONS`): `view` everyone; `converse` = staff rep/manager/admin and dealer_admin/dealer_reviewer;
`manage` = staff manager/admin and dealer_admin.

## Conversation workflow

1. **Inquiry** — website chat/inquiry form, ADF lead or Marketplace paste → message stored once; contact matched by
   channel identity, then e-mail; lead + conversation created; vehicle linked by stock/VIN (only this client's).
2. **Draft** — job `conversation_process`: language detection (EN/ES), intent and risk analysis, verified fact sheet,
   deterministic composer; optional Claude rephrasing (off unless `DF_AI_ENABLED`; discarded if it adds facts);
   output validator (no unsupported numbers, financing/warranty/history claims or appointment promises).
3. **Decide** — draft-only unless all automation gates pass (mode B/C authorization + passing evaluation + onboarding
   gate + approved auto-send template + business hours + limits + no escalation/pause/takeover/opt-out).
4. **Review** — staff see the DRAFT (facts used, unresolved items, why it was held), edit and approve; text that the
   validator flags needs an explicit acknowledgement (audited).
5. **Send** — job `conversation_send` re-checks emergency stop, opt-out, suppression, pauses, channel; re-validates
   automatic/system text against current facts; delivers through `track_integration`. Website chat is SIMULATED unless
   live is authorized; ADF/Marketplace replies are recorded as sent by staff outside the platform.
6. **Escalate / notify / summarize** — financing, credit, warranty, trade-in, negotiation, condition, complaints,
   frustration, human request, sensitive data, stale/conflicting/missing data, ambiguous vehicle.
7. **Test drive** — a request with a time in business hours creates `pending_confirmation`; the reply says it is not
   confirmed; staff confirm (customer message + reminders queued through the same pipeline), decline or cancel.
8. **Opt-out** (STOP/ALTO, EN/ES phrases) — conversation and contact marked, suppression added, pending drafts
   superseded, queued messages blocked; nothing further is sent.

## Providers: simulated vs live

| Capability | Default | Live requires |
|---|---|---|
| Website chat replies | SIMULATED (recorded, never shown to the visitor) | `DF_CUSTOMER_MESSAGING_LIVE_ENABLED=true` **and** the client's `customer_messaging`/`website_chat` connection `connected` (with an authorization note) |
| ADF lead intake | Real intake (inbound only) | A lead source configured to POST to the secret URL |
| Marketplace | Manual (no API exists) | — |
| Staff e-mail (new lead, escalation, test drive) | SIMULATED via the mock provider | Phase 4 client e-mail gate (`DF_CLIENT_EMAIL_SENDING_ENABLED`, real provider, `report_email` connection) |
| Claude rephrasing | Off | `DF_AI_ENABLED=true` + `DF_ANTHROPIC_API_KEY`; missing/invalid key falls back to the grounded draft |
| Messenger, SMS, dealership calendar | Not available | Not ported (see above) |

## Testing

`pytest` (SQLite) and `DF_TEST_PG_URL=postgresql+psycopg://… pytest` (PostgreSQL). Phase 3 test files:
`test_conv_pipeline.py`, `test_conv_outbound.py`, `test_conv_security.py`, `test_conv_appointments.py`,
`test_conv_migration.py`, `test_conv_pages.py`, `test_conv_data_lifecycle.py`, plus `test_security_hardening.py`.
The 46-case evaluation runs inside the suite (`test_evaluation_set_passes`).
