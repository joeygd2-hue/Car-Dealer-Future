# Stage 1 — merge Phase 2 into the Phase 1 base

_Written before the merge. Branch `claude/integration-dealerflow`, created from Phase 1 head `427b137`
(tag `archive/phase1-427b137`). Merging Phase 2 head `f174a06` (tag `archive/phase2-f174a06`)._

## Affected scope

| Area | Change brought by Phase 2 |
|---|---|
| **Tables (new, 14)** | `clients`, `inventory_sources`, `import_runs`, `import_raw_records`, `vehicles`, `vehicle_photos`, `vehicle_changes`, `dealer_listing_settings`, `listings`, `listing_revisions`, `listing_reviews`, `channel_connections`, `channel_publications`, `publish_operations` — every one carries `client_id`; children use composite `(client_id, id)` foreign keys so a row can't point at another dealership's parent. |
| **Tables (altered, 1)** | `users`: adds nullable `client_id` (FK `clients.id`) and `client_role` (`dealer_admin`/`dealer_reviewer`/`dealer_viewer`). Staff users keep both NULL. |
| **Migrations** | Adds `0002_phase2_inventory_listings` on top of Phase 1's `0001`. Additive only (create tables, add 2 nullable columns). Downgrade drops them. |
| **APIs / routes** | New `/dealers/...` workspace (inventory, imports, feeds, listings, approvals, publishing, reports, settings, users, CSV export) and `/api/v1/dealers/...` JSON API. Existing Phase 1 routes unchanged. |
| **Jobs** | `dealer_jobs`: inventory feed sync, listing generation, publishing operations, hourly stale-vehicle scan, scheduled from `worker.schedule_tick`. Same `jobs` table and claim logic as Phase 1. |
| **Auth rules** | A user with `client_id` set is a dealership user: confined to `/dealers`, `/api/v1/dealers`, `/logout`; `/` redirects to their workspace; staff-role checks reject them. `tenancy.tenant_session` adds loader criteria + a flush guard so a dealer-scoped session can't read or write another client's rows. |
| **Config** | New optional settings (listing generation, publishing channels, feed limits). No renamed or removed settings. |

## Conflicts expected (trial merge)
`git merge --no-commit f174a06` reported 4 text conflicts: `.gitignore`, `README.md`,
`docs/progress/build-status.md`, `dealerflow/web/deps.py`. Resolution plan:
- `deps.py`: **keep both** — Phase 1's password-version session revocation (security fix `d6da669`) and
  Phase 2's dealer-user confinement. Both checks run in `current_user`.
- `.gitignore`, `README.md`, build-status: union of both sides; test counts updated to the measured result.

## Data-loss risk
None. The migration only adds tables and nullable columns; no existing table is rewritten or dropped. No
production database exists on any branch (all data synthetic).

## Known overlaps (not fixed in this stage)
- Two SSRF guards: Phase 1 `integrations/website.py` (with the DNS-rebinding fix) and Phase 2
  `services/netguard.py` (feeds). Both kept; unify in the cross-cutting stage.

## Gate
Phase 1 suite (136) + Phase 2 additions must pass; `alembic upgrade head` from empty must succeed and
match the models.

## Result (after the merge)
- Conflicts resolved as planned (both `deps.py` checks kept: password-version revocation **and** dealer
  confinement, for cookie sessions and API keys).
- Added `pythonpath = ["."]` to the pytest config: the merged suite imports `tests.conftest` /
  `tests.dealer_helpers`, which only worked when pytest happened to run from the repo root via `python -m`.
- **SQLite: 183 passed** (136 Phase 1 + 47 Phase 2), including `test_migrations_match_models`.
- **PostgreSQL 16: 181 passed, 2 failed.** Both failures are test-harness problems, not product defects, and
  both are already fixed on the Phase 4 head that stage 2 merges: `test_sqlite_backup_and_restore` is
  SQLite-only (fixed in `194d247` by skipping on Postgres) and `test_authorised_feed_sync_through_worker`
  patched every DNS lookup, including the Postgres client's (fixed in `13e6ea4`).
- No migration conflict, data-loss path or security regression found.
