# Build status

Last updated: 2026-10-18 by the Phase 2 chat. Branch: `claude/dealership-inventory-publishing-iq0aqw`.

## Starting point (verified)
When Phase 2 started, the repository (all branches, no PRs) contained only `README.md`. **No Phase 1 code
(CRM, outreach, scheduling) existed.** Phase 2 therefore built the shared foundation, then the user asked for
a persistent outreach agent (Phase 1 scope), which is now also built on that foundation. The user chose
Python + FastAPI for all phases; see `docs/architecture/stack-and-interfaces.md`.

## Test results
`python -m pytest` → **80 passed** (2026-10-18, Python 3.13, SQLite). No network access in tests: HTTP providers
use `httpx.MockTransport`, outreach uses sandbox providers and simulated prospects, AI uses a mocked client.

| File | Tests | Covers |
|---|---|---|
| tests/test_inventory.py | 11 | CSV/XLSX import, normalisation, malformed rows, contradictory data, VIN check digit, duplicates in file and across sources, idempotent re-import, price change, sold/removed detection, mass-removal safeguard, authorised feed via worker, stale scan, SSRF guard on feed URLs |
| tests/test_listings.py | 18 | Bilingual generation from verified facts, worker generation, guardrails (urgency EN/ES, unsupported warranty/accident/title/owner/financing claims, mileage/year/price/drivetrain mismatches, keyword stuffing), supported claims allowed, required fields and disclaimers, warning acknowledgement, stale-facts approval refusal, agency approval gating, batch safeguards, immutable revisions, AI output still guard-railed |
| tests/test_publishing.py | 9 | Marketplace manual workflow (URL + attestation required), price change → needs update, sold → withdrawal required, API channel submit/confirm, idempotent publish requests, retries with backoff, permanent failure + manual retry, sold → API delete, cannot publish stale/unapproved, Meta adapter global guard + request shape |
| tests/test_isolation.py | 5 | API cross-dealer reads/writes → 404, list scoping, server-side roles, ORM tenant filter + cross-tenant write refusal + client_id immutability, composite FK enforcement |
| tests/test_jobs.py | 7 | Idempotent enqueue, backoff → dead, non-retryable, crash recovery via lease expiry, single claim, schedule once-per-slot and restart behaviour, heartbeats |
| tests/test_outreach.py | 29 | Discovery/enrichment/qualification (franchise disqualified, no duplicates), approval-gated first touch with CAN-SPAM checks, opt-out reply stops sequence, suppression blocks approved send, unsubscribe link, positive reply → slots → auto-booking, booking approval by default, approved-fact answers vs escalation, classifier (13 cases incl. Spanish and conflicting signals), bounce, hourly limit + budget, schedule window, duplicate prevention + interrupted-send handling, provider failure retries/escalation, live provider blocked without authorisation, emergency stop/pause, agent recovery after worker crash, dashboard measured vs estimated |
| tests/test_api_flow.py | 1 | Full dealership flow over HTTP: settings → upload → worker drafts → batch approve → Marketplace package + manual confirmation → sandbox publish → reports → ES CSV export → audit trail |

Manual verification (2026-10-18): seeded fixture data (`python -m scripts.seed_fixtures`), ran the real worker
process (`python -m app.worker`) which generated drafts, ran `uvicorn`, and drove the UI in headless Chromium:
login, inventory, listing review, approval, Marketplace package, recording publication, reports, imports,
outreach dashboard, mobile layout. No console errors. Alembic `upgrade head` on an empty database creates all 36 tables.

## Implemented

### Shared platform (all phases)
- FastAPI app, bearer-token auth (scrypt passwords, hashed session tokens), roles and per-dealership memberships.
- Tenant isolation: tenant-bound ORM sessions, composite FKs, client_id immutability, 404 for foreign ids.
- Append-only audit log with cost field; AES-GCM credential encryption; security headers + CSP; SSRF guard on
  server-side fetches of user-supplied URLs.
- Durable DB-backed job queue: idempotency keys, leases, crash recovery, exponential backoff, dead jobs,
  recurring schedules, worker heartbeats, `/api/health` and `/api/system/status`.
- Claude wrapper (structured outputs, refusal fallback, measured token cost). Disabled without a key.
- Alembic migrations; seed script with labelled fixtures; vanilla-JS SPA.

### Phase 2: inventory, listings, publishing
- Ingestion: CSV/TSV, XLSX, JSON; DMS exports via column mapping; authorised HTTPS feeds (encrypted credential,
  authorisation note required); manual entry. Raw rows preserved; run history; per-row outcomes.
- Normalised vehicle schema incl. dealer-reported title status/owner count (never inferred).
- Validation: missing/contradictory/implausible data, VIN structure and model-year cross-check, branded titles,
  photo URL checks, duplicate VINs, stale detection.
- Sync: change detection (price/details/photos/status), removals for full snapshots with a mass-removal safeguard,
  reappearance, scheduled feed sync, hourly stale scan, overdue confirmation flags.
- Listing generation: deterministic EN/ES template generator (default) and optional Claude generator constrained
  to verified facts; titles, descriptions, features, search terms, CTA, 7 FAQ answers, disclaimers; uncertainty notes.
- Dealer settings: brand voice, contact, hours, languages, disclaimers, financing statement, title template,
  character limits, required fields, review rules, enabled channels.
- Guardrails on every revision (generated or edited); immutable revisions with facts snapshot and hash.
- Review workspace: browse, preview EN/ES side by side, edit, photos, issues, approve with warning
  acknowledgement, reject, request regeneration, revision history, batch approval with preview/expected-count/confirm.
- Channel adapters: Facebook Marketplace manual workflow, Meta catalog API (guarded), sandbox; CSV export.
- Reporting per dealership (only verifiable metrics).

### Outreach agent (requested system-wide feature; Phase 1 scope)
- CRM models: prospects, contacts with evidence, observations with evidence, campaigns, enrollments, messages,
  suppressions, meetings, escalations, approved company facts, cost ledger, agent runs.
- Agent tick (bounded phases with checkpoints): inbox → recover → follow-ups → drafting → sending → replenish.
- Discovery (sandbox, Google Places, CSV import with attribution), polite website enrichment, qualification + scoring.
- Policy gate before every send (see `app/outreach/policy.py`): emergency stop, pause, campaign state, email-only,
  suppression, prospect/enrollment state, approval, schedule window, hourly/daily/mailbox limits, daily budget,
  integration health, live authorisation (env + integration + campaign), simulated-prospect isolation,
  CAN-SPAM content checks, banned claims.
- Reply handling: rules classifier (+ optional AI), opt-out/complaint suppression, bounces, OOO, referrals,
  sensitive escalation, approved-fact answers, meeting offers from free/busy, slot selection, booking with video link.
- Controls: global pause, per-campaign pause, emergency stop (cancels queued jobs, holds drafts), live authorisation
  with typed confirmation. One-click unsubscribe endpoint. Operator dashboard (measured vs estimated).

## Not yet verified end to end
**Autonomous outreach has not been verified with real integrations.** The persistent worker, scheduler,
retries, recovery and policy gates are verified with sandbox providers only. Gmail, Google Calendar, Google Places
and Meta catalog adapters are implemented against documentation and mocked in tests; none has run against the real
service. See `known-limitations.md`.

## Next steps
1. Phase 1 chat: reconcile with this foundation (reuse `prospects`/campaign models or migrate onto them; do not add a
   second auth or queue). Obtain a Google Workspace mailbox + OAuth app (Internal) and test Gmail/Calendar live with
   an internal test recipient, then a supervised pilot campaign with first-touch approval on.
2. Phase 3 chat: client portal on the existing roles/`client_ctx`; customer messaging must treat Marketplace
   conversations as manual (no API). Use `dealer_listing_settings` as the approved-facts source.
3. Phase 4 chat: PostgreSQL deployment (+ optional row-level security), backups, monitoring, security review,
   SEO/analytics on `audit_log`, `vehicle_changes`, `cost_ledger`.
4. Obtain Meta App Review + a test catalog to verify `meta_catalog` live; add NHTSA vPIC enrichment; consider Google
   vehicle ads feed.
