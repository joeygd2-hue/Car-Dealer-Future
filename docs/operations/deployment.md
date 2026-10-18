# Deployment and operations

## Processes
The application has two long-running processes that share one database:

| Process | Command | Purpose |
|---|---|---|
| Web | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | API + SPA + public unsubscribe endpoint |
| Worker | `python -m app.worker` | Durable jobs: inventory sync, listing generation, publishing, stale scans, outreach agent ticks |

Run at least one worker. Several workers are safe (jobs are leased). If the worker stops, `/api/health` reports
`worker: not_running` and queued work waits; nothing is lost. On restart, expired leases are recovered and retried.

## Setup
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ",postgres" for PostgreSQL
cp .env.example .env                # set APP_ENCRYPTION_KEY etc.
alembic upgrade head                # create/upgrade the schema
python -m scripts.seed_fixtures     # optional: labelled demo data + logins
uvicorn app.main:app --reload       # terminal 1
python -m app.worker                # terminal 2
python -m pytest                    # tests
```
Production: `DATABASE_URL=postgresql+psycopg://...`, run behind HTTPS, set `PUBLIC_BASE_URL` to the public HTTPS
origin, keep `META_LIVE_PUBLISHING_ALLOWED` and `OUTREACH_LIVE_ALLOWED` false until each integration is verified.

## Schema changes
Edit models in `app/models/`, then `alembic revision --autogenerate -m "describe change"`, review the file, commit it,
and run `alembic upgrade head` on deploy. Tests create tables directly from the models.

## Health and monitoring
- `GET /api/health`: database + worker liveness (unauthenticated, no sensitive data).
- `GET /api/system/status` (platform staff): worker heartbeats, queue counts, recent dead/failed jobs, integration flags.
- `GET /api/outreach/dashboard`: agent state, last tick, measured results, costs, integration health.
- Structured logs from uvicorn and the worker; audit trail in `audit_log` (append-only).

## Runbooks
**Stop all outreach now:** Outreach agent → Emergency stop (or `POST /api/outreach/emergency-stop`). This cancels
queued outreach jobs and holds unsent drafts. Clearing it requires a platform admin; held drafts must be re-approved.

**Pause temporarily:** "Pause all" (global) or set a campaign to `paused`.

**Going live with a mailbox (after testing):** create the Gmail integration with an encrypted OAuth refresh token,
set `OUTREACH_LIVE_ALLOWED=true`, authorise the integration (typed confirmation), authorise the campaign, keep
first-touch approval on, use low daily limits, and send to internal test addresses first.

**A feed sync keeps failing:** check Imports → Sources (last status) and `/api/system/status` dead jobs; the source's
`last_status` is `failed` with the error in the audit log. Fix credentials/URL and click "Sync now".

**"Removal blocked" on an import:** a full-snapshot file was missing most vehicles. Confirm the file was complete;
re-import a complete file or mark vehicles sold/removed individually.

**Vehicle sold:** mark it sold (or let the feed report it). API channels are deleted automatically; manual channels
show "Withdrawal required" until staff remove the Facebook listing and record it.

## Backups and secrets
Back up the database (and keep `APP_ENCRYPTION_KEY` safe; without it stored credentials cannot be decrypted).
Rotate a provider credential by saving a new secret on the connection/integration. Never put secrets in config JSON
fields; the API strips keys containing "token", "secret" or "password" from config JSON.
