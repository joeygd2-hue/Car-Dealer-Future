# Deployment and recovery results (2026-10-18)

All runs used mock providers, synthetic or SIMULATED prospects, and no real credentials.

## Environment

- Docker 29.8.2 (daemon started inside the audit sandbox), image built from the repository `Dockerfile`.
- Sandbox-only workaround: the sandbox intercepts HTTPS with its own certificate authority, so the
  audit build used an uncommitted overlay Dockerfile that adds that CA for `pip install`. The base
  image was pulled from `mirror.gcr.io` because Docker Hub returned 429. Neither change is needed on
  a normal host.
- Image: `python:3.12-slim`, 277 MB, runs as uid 10001.

## Results

| Check | Result | Status |
|---|---|---|
| Image builds from the repo Dockerfile | Built (with the sandbox-only CA overlay) | Partially verified (not built on a normal host) |
| Production refuses to start without `DF_SECRET_KEY` | `RuntimeError: DF_SECRET_KEY must be set outside development` | Verified |
| `migrate` on an empty volume, and again on an existing one (idempotent) | Both succeed | Verified |
| `web` + `worker` containers on a shared volume | `/healthz` ok, worker heartbeat ~4 s, container HEALTHCHECK `healthy` | Verified |
| Non-root container user | uid 10001 | Verified |
| API docs hidden in production | `/api/docs` → 404 | Verified |
| Security headers | nosniff, DENY, same-origin, CSP, HSTS (production) | Verified |
| Graceful worker stop (SIGTERM) | Stops in 0.3 s, exit 0, logs "worker stopped" | Verified |
| Crash recovery: job left `running` by a dead worker | Reclaimed after the 15-min stale-lock window and completed | Verified |
| Crash recovery: agent tick interrupted mid-run | Marked `error: interrupted (worker restarted)`; next tick ran normally | Verified |
| Data survives a full restart | 7 prospects before and after | Verified |
| Backup while services run (SQLite online backup) | Integrity-checked single file, mode 600, owned by uid 10001 | Verified |
| Restore refuses without `--yes` | Refused | Verified |
| Restore after simulated data loss (all prospects deleted) | 7 prospects and 17 audit rows restored; app healthy afterwards | Verified |
| Postgres 16 backup/restore (`pg_dump -Fc` / `pg_restore --clean`) | 7 prospects and 8 audit rows restored after deletion | Verified (local Postgres, outside Docker) |
| Full test suite on Postgres 16 | 125 passed, 1 skipped (SQLite-only backup test) | Verified |
| Migrations upgrade → downgrade → upgrade (SQLite, Postgres) | Clean | Verified (Phase 1) |
| Docker Compose with file-based secrets (`/run/secrets`) | Secret read from file; `DF_SECRET_KEY` absent from the container environment | Verified (found and documented that secret files must be owned by uid 10001) |
| Secret leak scan of web + worker logs | 0 occurrences of the secret key | Verified |
| HTTPS / reverse proxy | Not tested (no domain or certificate) | Unverified |
| Scheduled backups and off-host copies | Not configured (operator) | Unverified |

## Recovery runbook

1. **Worker died:** restart it. Stale jobs are reclaimed after 15 minutes. A message left in `sending`
   becomes `failed` ("outcome unknown"); check the mailbox before re-approving it.
2. **Bad deploy:** `docker compose down`, check out the previous image tag, `up -d`. Migrations are forward-only in
   production: restore the pre-deploy backup instead of downgrading.
3. **Data loss or corruption:** stop `web` and `worker`, then run `python -m dealerflow restore /backups/<file> --yes`
   and start them again. The previous file is kept as `<db>.pre-restore`.
4. **Suspected credential leak:** see the revocation runbook in `security-findings.md` (S1).
5. **Something is sending that shouldn't:** Agent page → **EMERGENCY STOP**. It blocks every provider call immediately
   and returns queued messages to draft.

Recommended schedule: a nightly `backup`, copied off-host encrypted, with 30 daily and 12 monthly copies kept. Do a test
restore monthly.
