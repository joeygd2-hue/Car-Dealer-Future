# MyBot architecture decisions (2026-10-19)

The owner set these working defaults on 2026-10-19. Each records the existing implementation conflicts that were found
**before** code changed. "Current" means branch `claude/mybot-unification` at `c68a40d`, plus the source branches
at the commits listed in §0.

## 0. State at the start of this round

| Branch | Commit | Relationship to the unified branch |
|---|---|---|
| `claude/mybot-unification` | `c68a40d` | Phase 1.5 + Phase 2 + unification fixes; 204 tests passing (SQLite) |
| `claude/blissful-davinci-82py6w` (Phase 4) | `194d247` | **Moved since the feature matrix was written.** It merged Phase 1.5 (`427b137`) and Phase 2 (`f174a06`) itself, unified the two `clients` tables, renumbered migrations to `0001`–`0004`, and dropped its interim inventory/listing tables. It shares history with the unified branch, so it is **merged** rather than hand-ported (porting-plan increment 3) |
| `claude/magical-darwin-nncp64` (Phase 3) | `98fa3d3` | Still a separate `app/` package (6 new commits: crash-takeover race fix, clean-setup fixes, stress results). Ported in increments |
| `claude/charming-ritchie-358e1g` (Phase 1.5) | `427b137` | Unchanged; already merged |
| `claude/dealership-inventory-publishing-iq0aqw` (Phase 2) | `f174a06` | Unchanged; already merged |

## D1. Tenancy: one dealership per customer account (MVP), extensible to groups

**Decision.** A dealership user belongs to exactly one dealership (`users.client_id`, role in `users.client_role`).
Staff users have `client_id = NULL`. Groups and multi-location are deferred, but nothing may assume a dealership has a
single location or that the `users.client_id` column is the only way to grant access.

**Existing implementation and conflicts.**
- Phase 2 and Phase 4 (after its merge) already use `users.client_id` + `client_role`. **No conflict.**
- Phase 3 uses `Membership` (user ↔ dealership, many-to-many). **Conflict:** not ported. Phase 3 code that assumes
  memberships is adapted to `ClientContext` / `authorize_client`.
- **Extensibility rule:** all access checks go through `tenancy.authorize_client(...)` (one place), so a future
  `client_memberships` table and a `locations` table can be added without touching every route.

## D2. Customer portal at `/portal`

**Decision.** `/portal` is the dealership-facing workspace. Existing working routes keep working through safe redirects.

**Existing implementation and conflicts.**
- Phase 2's dealership workspace (inventory, listings, publishing, settings, reports) lives at **`/dealers/{client_id}/…`**
  (35 routes), plus the JSON API at `/api/v1/dealers/{client_id}/…`.
- Phase 4's client portal (reports, invoices, tickets) lives at **`/portal`** and `/api/v1/portal/…`.
- **Conflict:** two dealership-facing areas with different URL schemes, and dealer users are allowed into both prefixes.
- **Resolution:**
  - The Phase 2 workspace moves under **`/portal/{client_id}/…`**. Phase 4's pages stay at `/portal`, `/portal/reports/…`
    and `/portal/tickets`.
  - `GET /dealers/…` answers with a redirect to the same path under `/portal/…`. Non-GET requests to the old prefix are
    refused with 410 rather than redirected (a redirected POST would drop the form or the CSRF context).
  - `/api/v1/dealers/…` stays as is: it's an API contract, and is documented as such.
  - A dealer user can only reach their own `client_id`. Redirects never change the client id, and the existing
    `authorize_client` check applies after the redirect.

## D3. Naming: `dealerflow` package, **MyBot** brand

**Decision.** The Python package, CLI (`python -m dealerflow`), environment prefix (`DF_`) and database names stay.
Everything a person sees (page titles, header, login page, portal, unsubscribe page, report emails) says **MyBot**.

**Existing implementation and conflicts.**
- Templates show "dealerflow" in the title and header (`templates/base.html`). This changes to MyBot.
- Outreach emails are sent in the operator's own business name (`DF_BUSINESS_NAME`), as CAN-SPAM requires.
  **Not changed:** MyBot is the product, not the sender identity.
- Technical identifiers (log logger names, User-Agent `dealerflow-research/…`, backup file names) stay; they aren't
  customer-facing. Docs refer to "MyBot (package `dealerflow`)".

## D4. Client report delivery requires configuration **and** approval

**Decision.** Reports may be generated automatically, but delivery to a client needs (a) the global switch,
(b) a connected email integration for that client, and (c) a staff approval of that specific report. No report goes to
a real customer during this round.

**Existing implementation and conflicts.**
- Phase 4 `monthly_reports_job` **emails reports automatically** when `monthly_report_email` automation is enabled for a
  client, `DF_CLIENT_EMAIL_SENDING_ENABLED=true` and a `report_email` connection is `connected`. **Conflict:** no
  per-report human approval.
- **Resolution:**
  - The job only drafts reports.
  - Sending requires a manager to approve that report version.
  - The send path re-checks the switch, the connection, the emergency stop (through `track_integration`) and the approval.
  - The approval is audited.
  - A regression test proves an unapproved report is never sent, even with every switch on.

## D5. Staged integration with controls and tests at every stage

**Decision.** Continue in small, separately committed stages. Each stage keeps every control in
`control-preservation.md` passing, adds regression tests for controls it touches, and runs the full suite before push.

**Existing implementation and conflicts.**
- Phase 4's own audit found weaknesses that a plain merge would bring in. Each is fixed in the merge stage, with tests:
  1. SEO crawler without an SSRF guard.
  2. CSRF on PATCH (already fixed on this branch).
  3. Production config check downgraded to warnings.
  4. `RedactingFilter` never attached to logging.
  5. The in-process API rate limiter's memory grows without bound.
- Phase 3's interim tables in Phase 4's migration `0004` (`conversations`, `conversation_messages`, `appointments`) will
  collide with the real Phase 3 models. They're replaced by a later migration when Phase 3's customer model is ported,
  never edited in place.

## Out of scope this round (owner instruction)

AI receptionist expansion and the MyBot Flipper product are **not started**. Phase 3's existing customer-response
engine is ported only as built, without new capabilities.
