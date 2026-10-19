# Launch-readiness audit (Phase 1.5)

Date: 2026-10-18 · Branch `claude/charming-ritchie-358e1g` · Auditor: Claude Code session

**Verdict: not ready for live outreach.** The software controls now hold up under adversarial testing,
and the deployment runs and recovers in Docker. But no real integration has been verified, secrets aren't
encrypted at rest, there's no HTTPS host, and two legal questions are open. Nothing was sent, called, booked
or bought during this audit.

Status key: **Verified** = demonstrated by a test or an actual run. **Partially verified** = code and mocked tests
pass, live confirmation pending. **Unverified** = not checked. **Blocked** = needs something from you (credentials,
a decision, counsel).

Detail documents in this folder:
[deployment-recovery](deployment-recovery.md) · [security-findings](security-findings.md) ·
[integration-verification-matrix](integration-verification-matrix.md) · [compliance-checklist](compliance-checklist.md) ·
[lead-sourcing](lead-sourcing.md) · [pilot-plan](pilot-plan.md)

## 1. Repository state

| Check | Result | Status |
|---|---|---|
| Branch, working tree, remote | `claude/charming-ritchie-358e1g` clean and in sync with origin at audit start (`fc1b9bb`) | Verified |
| Reported 102 tests | 102 passed at audit start; **136** after this audit (SQLite); 135 + 1 SQLite-only skip on Postgres 16 | Verified |
| Latest commit matches its description | `fc1b9bb` matched. **But** `7f0db48` ("Note unverified Docker build…") also committed 83 stale `build/lib` files | Fixed |
| Docs vs implementation | See the discrepancy list below | Fixed / documented |
| Other phases | `blissful-davinci` (Phase 4) and `magical-darwin` (Phase 3) built **separate `app/` packages** from the initial commit, each with its own auth, database and outreach agent. `dealership-inventory-publishing` (Phase 2) merged onto `dealerflow` | **Blocked: needs a merge plan** (see §7) |

### Discrepancies found between documentation and code

1. Emergency stop was documented as halting the agent, but it only blocked email sends. Bookings, inbox polling, research, calls and discovery still ran. **Fixed** (central gate).
2. Pausing a campaign stopped new drafts but **not messages already queued** for it. **Fixed.**
3. "Every external call is logged": failed calls inside background jobs were lost on rollback. **Fixed.**
4. TxDMV import was described as supported via "TxDMV-style columns". The real file is `.xls`, with different headers and 21k rows; the old importer could not have read it. **Fixed** with a dedicated profile.
5. Bounce detection was documented, but mailer-daemon bounces could not be matched to messages. **Fixed.**
6. Zoom scope instructions were vague. **Now specific** (secondary-source names, flagged).
7. Docker was documented as unbuilt. **It has now been built and run.**
8. build-status said Phases 2–4 "not started": they are in progress on separate branches (see §7).
9. The README claimed the setup steps were enough to go live, but no backup, restore or retention tooling existed. **Added.**

## 2. Deployment audit (details: deployment-recovery.md)

| Item | Status |
|---|---|
| Docker image build | Partially verified (built with a sandbox-only CA overlay; not on a normal host) |
| Full stack in a clean environment (migrate, web, worker) | Verified |
| Migrations (fresh, re-run, up/down; SQLite and Postgres) | Verified |
| Restart and crash recovery (stale jobs, interrupted ticks, persistence) | Verified |
| Authentication, roles, CSRF, API keys | Verified (`tests/test_web.py`, `tests/test_adversarial.py`) |
| Audit logging of external actions | Verified (`test_every_external_state_change_has_an_audit_entry`) |
| Backups and restoration | Verified (SQLite in container, Postgres locally) |
| HTTPS, reverse proxy, scheduled off-host backups | **Unverified**: depends on a host you choose |

## 3. Security (details: security-findings.md)

13 findings fixed (F1–F13). Open: **S1 secrets not encrypted at rest (production blocker; mitigation plan written)**,
S2 no MFA, S6 base image not pinned or scanned, S7 TLS proxy not deployed, S8 data at rest unencrypted, plus low items.
`pip-audit`: no known vulnerable dependencies. Log scan: no secrets.

## 4. Autonomous agent audit (adversarial tests: `tests/test_adversarial.py`)

| Control | Result | Status |
|---|---|---|
| Emergency stop immediately prevents new external actions | Every provider call is refused (and logged as `blocked`); queued messages return to draft; jobs defer; only an admin can clear it | Verified |
| Pausing a campaign prevents further sends | Queued messages for a paused campaign are held, then sent after resume | Verified |
| Budgets enforced server-side | Discovery is skipped when the estimate would exceed the daily or monthly budget | Verified |
| Operating hours enforced server-side | Drafting and replies only in-window; agent-approved sends are held outside it | Verified |
| Suppression enforced before every action | Checked at draft, approval and send; opt-out arriving between approval and send cancels the message | Verified |
| Approval requirements | Reps and viewers can't approve; only admins can enable auto mode, auto-answers or live operation; queued content can't be edited | Verified |
| Retries can't duplicate emails or meetings | Idempotent jobs; ambiguous sends are never resent; calendar IDs are deterministic; lost Zoom responses are recovered | Verified (mock) |
| Expired credentials and provider failures fail safely | Auth failure disables the provider until re-verified; transient errors back off; nothing is marked sent or booked without a provider response | Verified (mock) |
| No bypassing authorization or unapproved providers | Unknown provider names fail; real providers need the env flag plus admin live authorization; real providers refuse simulated or reserved addresses | Verified |
| Simulated activity never mistaken for results | Separate counters in dashboard, agent page and weekly report; simulated sends don't move real prospects; live-check tests count as test data | Verified |
| No invented information or unapproved promises | Only whitelisted, verified template fields; drafting stops when a value is missing; observations need person verification; answers only from approved text; sensitive topics escalate | Verified |
| External actions traceable | Every state-changing provider call has an audit entry; failures and refusals are in the integration log | Verified |

## 5. Integration readiness (details: integration-verification-matrix.md)

| Item | Status |
|---|---|
| Exact setup instructions (email, DNS, calendar, Zoom) | Verified (docs updated: `docs/architecture/integration-setup.md`) |
| Current API requirements, scopes, limits, terms | Verified from primary docs, except Zoom scope names and Zoom daily limits (secondary) |
| Delivery to controlled inboxes, unsubscribe, bounces, reply matching, calendar, Zoom, revocation | **Blocked: needs your credentials and test inboxes.** Tooling ready: `python -m dealerflow live-check` |
| Live sending | **Not enabled** (as instructed) |

## 6. Lead sourcing and legal (details: lead-sourcing.md, compliance-checklist.md)

| Item | Status |
|---|---|
| Real TxDMV file imported (dry run + scratch commit), mapping corrected | Verified |
| Provenance kept; uncertain details flagged unverified | Verified |
| Source terms, robots.txt, rate limits, retention | Verified in code; TxDMV use terms ⚖️ need counsel |
| Contact restrictions checked immediately before each action | Verified |
| Federal and Texas compliance checklist with sources | Done; **Chapter 302 (cold calling) blocked** pending counsel |

## 7. Remaining blockers and remediation priorities

| # | Blocker | Why it matters | Owner | Priority |
|---|---|---|---|---|
| 1 | **Secrets at rest (S1)**: move secrets to file-based or managed secrets on an encrypted host | Credential theft would let someone send as you | You + me | P0, before any live credential |
| 2 | **Live integration verification** (`live-check` steps) | Nothing proves real email, calendar or Zoom work yet | You (credentials, test inboxes) | P0 |
| 3 | **Production host**: HTTPS proxy, `DF_BASE_URL`, MFA proxy (S2/S7), nightly encrypted backups | Unsubscribe links and webhooks need public HTTPS; protects the admin UI | You (choose host/domain) + me | P0 |
| 4 | **Attorney review**: templates, Chapter 302, TxDMV list use, business-cell DNC | Legal exposure | You | P0 for calls; P1 for email |
| 5 | ~~**Phase branches diverged**~~ **Resolved in code 2026-10-19:** Phases 1, 1.5, 2, 3 and 4 run in one app on `claude/phase3-integration` (not yet merged into `main`) | Four apps would have meant four auth systems, databases and outreach agents | You: review and merge | P1 |
| 6 | Pin and scan the base image (S6); data-at-rest encryption (S8) | Supply chain and PII | Me | P1 |
| 7 | Service delivery for the first client (concierge vs Phase 3) | Can't sell what can't be delivered. Phase 3 is integrated but every customer channel is a sandbox: replies stay in the platform | You | P1, before the pilot |
| 8 | Contact verification workflow at scale (only 33 of 191 Fort Bend dealers have a business email) | Limits pilot volume | Me (UI to speed up verification) | P2 |

### Phase 3 production blockers (added 2026-10-19, Phase 3 integration)

| # | Blocker | Why it matters | Priority |
|---|---|---|---|
| 9 | **No live customer channel**: Messenger/Twilio send adapters were not ported; every reply is SIMULATED, so website-chat visitors never see a reply | Customer conversations cannot be delivered | P0 before offering conversation handling |
| 10 | **MFA and self-service password reset for dealership admins** (Phase 3 had them; the unified app does not) | A dealer admin authorizes automated replies to real customers | P0 before Mode B/C with real customers |
| 11 | **Meta App Review, Twilio A2P 10DLC, public HTTPS webhooks** | Required by the providers; webhooks verified only with self-signed payloads | P0 for those channels |
| 12 | **Customer-message legal review** (TCPA/Texas SMS consent wording, opt-out keywords, Spanish copy, data-deletion page) | Legal exposure on SMS and Messenger | P0 for SMS |
| 13 | Tamper-evident audit log (Phase 3 had an HMAC chain; the unified log is append-only by convention) | Evidence for authorization and send decisions | P1 |
| 14 | PostgreSQL row-level security not configured; tenancy relies on ORM scoping + composite keys | Defence in depth for car-buyer PII | P1 |

**Recommendation on Phase 2:** don't start new Phase 2 feature work on this branch until #5 is decided. The Phase 2
branch has already merged onto `dealerflow`; Phases 3 and 4 have not. Items 1–4 must be done before any live
outreach or the pilot. Item 5 must be done before more phase work, so nothing else gets built twice.
