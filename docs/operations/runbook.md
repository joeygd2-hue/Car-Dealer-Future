# Runbook

## Health and monitoring

| Check | How | Healthy |
|---|---|---|
| Web + DB | `GET /healthz` | `ok: true`, `db: ok` |
| Worker | `/healthz` → `worker_alive`, `worker_heartbeat_age_seconds` | alive; heartbeat < 2 min |
| Agent | Agent page; `/healthz` → `agent_status`, `last_tick_finished_at` | the state you expect; recent ticks while running |
| Failed jobs | Dashboard "failed jobs"; `SELECT kind, last_error FROM jobs WHERE status IN ('failed','dead')` | none, or understood |
| Alerts | Costs page → Alerts (budgets, spikes, failed webhooks, payment failures) | acknowledged |
| Integrations | Integrations page (`integration_log`) | recent `ok` entries |
| Tickets | Tickets page, filter `open` (system tickets come from failed SEO audits and payment failures) | triaged |

Logs are JSON lines on stdout (`ts`, `level`, `logger`, `msg` + fields). Secret-named fields are redacted.

## Common incidents

| Symptom | Likely cause | Action |
|---|---|---|
| `worker_alive: false` | Worker stopped/crashed | Restart it. Running jobs older than 15 min are released automatically; sends that may have happened are not blindly resent (Phase 1 design) |
| Outreach paused unexpectedly | Budget reached, outside hours, emergency stop | Agent page shows the reason; resume deliberately |
| Something is sending that shouldn't | — | Agent page → **EMERGENCY STOP** (returns queued messages to draft). Then set `DF_OUTREACH_SENDING_ENABLED=false` and restart |
| Stripe events not appearing | Secret mismatch, wrong endpoint, unmapped client | `webhook_events` table; status `failed` rows have `error`; check alerts |
| SEO audit failed | Site down, 429, robots disallow | Audit row `error`; a ticket is opened; retry later — never bypass robots or rate limits |
| Budget alert | Usage spike or estimate rates too low | Costs page; compare with provider billing; import billing CSV for verified figures |
| Customer message shows `uncertain` | Worker died mid-send, so delivery is unknown | It is never resent automatically. Check the channel (or ask the visitor), then on the conversation choose **It was delivered** or **Not delivered — re-queue** (re-queue only when you confirmed it did not arrive) |
| Customer message `blocked` | Emergency stop, global pause, opt-out, rate limit, onboarding gate, unsafe content | The reason is on the message. Fix the cause and send a new reply; blocked replies are not retried |
| Need to stop customer replies | — | Responses → **Pause automation** stops automatic and system replies for that dealership. Agent page → **EMERGENCY STOP** blocks every customer send, staff-approved ones included. (The platform-wide pause is the `conversations_global_pause` app setting; there is no UI for it yet) |
| Staff member lost their MFA device | — | They use a recovery code. If none remain, an admin clears `users.mfa_enabled`, `mfa_secret_encrypted` and `mfa_recovery_hashes` for that user in the database and asks them to re-enrol on first login; record who did it |
| A client can't log in | Wrong password, inactive, lockout | Lockout clears after 15 minutes; an admin can set a new password or reactivate on the client's page (Dealership users); new dealership users are created in the dealership's settings |

## Backups and restore

**PostgreSQL** (recommended):

```bash
pg_dump --format=custom --no-owner "$DATABASE" > dealerflow-$(date +%F).dump        # daily, keep 30+ days off-host
pg_restore --clean --if-exists --no-owner --dbname "$DATABASE" dealerflow-2026-10-18.dump
```

**SQLite**: stop the worker (or use the online backup API), then
`sqlite3 state/dealerflow.db ".backup 'dealerflow-$(date +%F).db'"`.

Also back up `DF_ATTACHMENTS_DIR`. Test a restore into a scratch database at least monthly: restore, run
`python -m dealerflow migrate` (should be a no-op), open `/healthz` and a client page.

Never restore over production while the worker is running.

## Data retention and deletion

* Car-buyer conversations: the daily `retention_purge` job removes contact names/details and channel identities,
  message bodies, drafts, summaries and appointment contact details/notes older than `DF_RETENTION_DAYS_CONVERSATIONS` (default 365 days).
  Counts remain for reporting. Opt-outs (`customer_suppressions`) are kept so they continue to be honoured.
* Legacy interim conversation tables (`legacy_*`, from migration `0005`) are not purged by that job; drop them
  only through the removal plan in `docs/architecture/phase3-integration.md`.
* Client data export (portability request): `services.maintenance.export_client(db, client_id)` returns every
  row the client owns as JSON.
* Client deletion: `services.maintenance.delete_client(db, client_id, confirm_name, actor, export_dir)` writes
  an export, then deletes the client; owned rows cascade. Expenses keep their amount but lose the client link.
  Run from a Python shell inside the app environment; it is deliberately not exposed in the UI.
* Prospect opt-outs (`suppressions`) are kept indefinitely so they continue to be honoured.
* Audit and integration logs are kept indefinitely; prune by date if your policy requires.

## Secrets rotation

| Secret | Effect of rotating |
|---|---|
| `DF_SECRET_KEY` | Logs everyone out **and invalidates unsubscribe links already sent** — avoid unless compromised |
| `DF_ENCRYPTION_KEY` | **Do not simply replace it**: receipts and MFA secrets encrypted with the old key become unreadable. No rotation command exists yet; keep the key backed up. If it is compromised, plan a re-encryption with both keys available |
| Provider keys/tokens | Update env, restart web + worker, re-verify on the Integrations page |
| `DF_STRIPE_WEBHOOK_SECRET` | Roll in Stripe with an overlap window, update env, restart |
| User API keys | Regenerate in Settings |

## Routine operations

* **Monthly**: reports are drafted on days 1-3 for active clients; review each draft, then publish to the
  portal or email it (email only if the client's automation and connection are authorized).
* **Monthly**: import provider billing exports on the Costs page so verified costs replace estimates.
* **Monthly**: reconcile the bank statement (Finance → Reconciliation).
* **Weekly**: review open tickets, alerts and failed jobs.
