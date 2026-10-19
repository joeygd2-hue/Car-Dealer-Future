# Car Dealer Future

AI-assisted marketing and operations platform for independent used-car dealerships (initial market: Sugar Land / Houston, TX).
**One application for all four phases.** Stack: Python 3.13 · FastAPI · SQLAlchemy 2 + Alembic · Jinja2 server-rendered UI · durable DB-backed job queue + worker · pytest.

> **Pilot status:** sandbox pilot ready; see [`docs/progress/pilot-readiness.md`](docs/progress/pilot-readiness.md).
> **Foundation decision pending:** `dealerflow/` (Phases 1, 2, 4 on other branches) vs this `app/` — see
> [`docs/architecture/foundation-decision.md`](docs/architecture/foundation-decision.md).
>
> Before changing anything, read [`docs/architecture/shared-stack.md`](docs/architecture/shared-stack.md) (shared contracts),
> [`docs/progress/build-status.md`](docs/progress/build-status.md), [`docs/progress/known-limitations.md`](docs/progress/known-limitations.md)
> and [`docs/architecture/integration-setup.md`](docs/architecture/integration-setup.md).

## Quick start (sandbox — nothing is sent anywhere)

```bash
uv sync --frozen             # or: pip install -e .   (add `--extra llm` for optional Claude rephrasing)
cp .env.example .env         # set ENCRYPTION_KEY (command in the file); keep LIVE_INTEGRATIONS_ENABLED=false
uv run alembic upgrade head  # create/upgrade the database schema (creates ./data/)
uv run python -m scripts.seed_demo          # synthetic demo dealership + logins (requires migrated DB)
uv run uvicorn app.main:app --reload        # web app  → http://localhost:8000
uv run python -m app.worker                 # persistent worker (scheduler + jobs) — run in a second terminal
```

- Operator/admin workspace: `/admin` · **Pilot console** (emergency stop, allowlist, ledger, reconciliation): `/admin/pilot`
- Outreach agent dashboard: `/admin/outreach` · Dealer portal: `/portal` · Account security (MFA, password): `/account/security`
- Website chat test page: `/chat/<public_chat_key>` (printed by the seed script)
- Health: `/healthz` (DB) and `/healthz/worker` (503 when no worker heartbeat within 90s)

## Tests & quality

```bash
uv run pytest            # 184 tests (unit, HTTP security, migrations, worker recovery, sandbox end-to-end)
uv run ruff check .
SMOKE_BASE_URL=http://127.0.0.1:8000 uv run python -m scripts.smoke_e2e   # against running web + worker (32 checks)
```
The response-engine evaluation (46 synthetic EN/ES conversations, `evals/synthetic_conversations.json`) runs inside the test
suite and from the UI (Admin → Evaluations, or Portal → Automation → Run evaluation). A passing run is required before a
dealership can enable automation Mode B or C.

## Layout

```
app/
  config.py, db.py            settings (env only), engine/session, utcnow()
  models/                     ALL tables (shared; extend, don't duplicate)
  security/                   sessions + CSRF + tenant authorization, scrypt passwords, Fernet credential encryption
  integrations/               provider contracts (base.py), sandbox adapters, real adapters, registry (live gating)
  services/
    jobs.py, ../worker.py     durable queue, retries/backoff, leases, scheduler, worker heartbeat
    responder/                bilingual grounded reply engine (analysis, facts, engine, safety)
    inbox/                    ingest/dedupe → process → outbound send; ADF/XML parser
    automation_policy.py      Modes A/B/C + pauses, rate limits, business hours, messaging windows
    escalation.py, summaries.py, appointments.py, notifications.py, evaluation.py, audit.py, controls.py
    outreach/                 persistent autonomous outreach agent (discovery, compliance, drafting, classify, booking, metrics)
  web/                        routers: auth, public (chat API, webhooks, ADF, booking, unsubscribe), portal, admin
  templates/, static/         UI + embeddable chat widget
migrations/                   Alembic (baseline 4122abfb2c23)
evals/                        synthetic evaluation conversations
docs/                         architecture, progress, research
```
