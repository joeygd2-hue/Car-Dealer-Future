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
| `services/notifications.py` | `services/conversations/notify.py` | In-app staff notifications; e-mail goes through Phase 4 `client_email.send` (SIMULATED unless authorized) |
| `security/mfa.py` | `security_mfa.py` | Stdlib TOTP + single-use recovery codes, secrets Fernet-encrypted |
| `web/public.py` (chat API, chat page, ADF intake) | `web/routes_conversations_public.py` | Per-client public key; DB rate limits; CORS limited to the client's website |
| `web/portal.py` (inbox, conversation, appointments, content, automation, business info) | `web/routes_conversations.py` under `/dealers/{id}/…` | Phase 2 `dealer_ctx` permissions; new `converse` permission |
| `evals/synthetic_conversations.json` (46 cases) | `dealerflow/evals/` | Gate for modes B/C, run against an isolated in-memory DB |
| Phase 3 tests | `tests/test_conv_*.py` | Rewritten against dealerflow fixtures |

## Deliberately not ported (documented as limitations)

| Phase 3 item | Reason |
|---|---|
| `app/` auth, sessions, users, memberships, jobs, audit, outreach agent, CRM | dealerflow already has stronger or equivalent versions (Phase 1/1.5/2). One user = one dealership (Phase 2 rule) |
| Meta Messenger and Twilio SMS adapters + webhooks | Code-only in Phase 3, never verified; need Meta App Review / A2P 10DLC. Not ported to avoid unverified live code; channels stay listed as "not connected" |
| Per-dealership Google Calendar for test drives | Phase 3 only had a sandbox calendar for this; availability is recorded as `no_calendar` and staff confirm manually |
| Pilot allowlist console, send-attempt ledger table | dealerflow keeps live customer messaging off entirely (no live adapter). The message row itself is the ledger (`sending` committed before the provider step; a stale `sending` becomes `uncertain`, never re-sent) |
| HMAC-sealed audit log | Not in scope; noted as a remaining item |
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
