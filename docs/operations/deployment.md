# Deployment

Status: the app and worker were run locally from the package (SQLite) and the test suite passes on SQLite and
Postgres 16. **The Docker image has not been built or run in this environment** (no Docker daemon), and no
production deployment has been made.

## Requirements

* Python 3.11+ (developed on 3.12/3.13), or the `Dockerfile` (python:3.12-slim).
* PostgreSQL 14+ recommended (`pip install -e ".[postgres]"`). SQLite works for a single host.
* A TLS reverse proxy (Caddy, nginx, a platform load balancer) in front of the web process. Public HTTPS is
  required for unsubscribe links, Stripe/Zoom/Twilio webhooks and secure cookies.
* Persistent storage for SQLite (if used) and `DF_ATTACHMENTS_DIR` (expense receipts).
* Two long-running processes: `web` and `worker`. Run exactly one worker per database unless you have checked
  that your scale-out plan is safe (job claims are atomic, but rate limiters are per process).

## Steps (single host, Docker Compose)

1. `cp .env.example .env` and set at least: `DF_SECRET_KEY` (≥32 random chars:
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`), `DF_BASE_URL=https://your.domain`,
   `DF_DATABASE_URL`, `DF_BUSINESS_NAME`, `DF_BUSINESS_POSTAL_ADDRESS`, `DF_SENDER_NAME`, `DF_SENDER_EMAIL`.
   Leave every provider on `mock` and both sending flags `false` for the first deployment.
2. `docker compose up -d --build` runs `migrate` (Alembic upgrade + idempotent seed), then `web` and `worker`.
3. Create the first admin: `docker compose run --rm web python -m dealerflow create-user you@yourco.com admin`.
4. Put the TLS proxy in front of `127.0.0.1:8000`. Check `https://your.domain/healthz` returns
   `"worker_alive": true`.
5. Configure providers one at a time (`docs/architecture/integration-setup.md`), verify each on the
   Integrations page, then follow "Going live" below.

Without Docker: `pip install -e ".[postgres]"`, `python -m dealerflow migrate`, then run
`python -m dealerflow web` and `python -m dealerflow worker` under a process supervisor (systemd etc.).

## Migrations

* `python -m dealerflow migrate` applies Alembic revisions (`0001` Phase 1, `0002` Phase 4, `0003` interim
  Phase 2/3 tables) and seeds reference data. All Phase 4 revisions are additive; upgrade → downgrade →
  upgrade was verified with existing rows on SQLite and Postgres 16.
* Take a backup before every upgrade (see runbook). Roll back with `alembic downgrade <rev>` using
  `dealerflow.migrate.alembic_config()`, or restore the backup.

## Going live (in this order)

1. Real email for outreach: configure Gmail or SMTP, verify, set `DF_OUTREACH_SENDING_ENABLED=true`, then an
   admin authorizes live operation on the Agent page. Keep campaigns on manual approval at first.
2. Client email (reports, lead summaries): set `DF_CLIENT_EMAIL_SENDING_ENABLED=true`, then for each client
   record a `report_email` connection as `connected` with how it was authorized.
3. Stripe: create a **test-mode** webhook endpoint for `https://your.domain/webhooks/stripe` with events
   `invoice.paid`, `invoice.payment_failed`, `charge.refunded`; set `DF_STRIPE_WEBHOOK_SECRET`; send test
   events; only then repeat with live mode. Set each Stripe invoice's `metadata.client_id` or link
   `invoices.external_id` to the Stripe invoice id.
4. SEO: set `DF_PAGESPEED_API_KEY` (optional); register a client's site, record the authorization, run an audit.
5. AI (optional): `pip install -e ".[ai]"`, `DF_AI_ENABLED=true`, `DF_ANTHROPIC_API_KEY`.

## Production checklist

- [ ] `DF_ENV=production` (the app refuses to start with a weak secret key, or a non-https base URL once
      real email is enabled)
- [ ] PostgreSQL with automated backups and a tested restore
- [ ] TLS proxy; HSTS is sent by the app in production
- [ ] Attachments directory on an encrypted, backed-up volume
- [ ] One worker; `/healthz` monitored (alert if `worker_alive` is false for > 5 minutes)
- [ ] Logs (JSON lines on stdout) shipped somewhere searchable; alert on `"level": "ERROR"`
- [ ] SPF, DKIM and DMARC for the sending domain
- [ ] Admin accounts limited; MFA in front of the app if possible (not built in)
