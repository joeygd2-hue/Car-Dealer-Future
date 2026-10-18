# Car-Dealer-Future: dealerflow

Prospecting, CRM, outreach and a persistent outreach agent, for a business that sells marketing
automation to independent used-car dealerships around Sugar Land and Houston, Texas.

**Phase 1 (this code):** lead discovery and import, deduplication, explainable scoring, CRM pipeline,
human-approved email outreach (English/Spanish), reply import and classification, opt-out and
suppression enforcement, Zoom and calendar scheduling, click-to-call, daily and weekly reports, and
a persistent autonomous agent with pause and emergency stop.

> Out of the box every provider is **SIMULATED** (mock). Nothing reaches a real person until you
> configure real credentials, set `DF_OUTREACH_SENDING_ENABLED=true`, **and** an admin authorizes live
> operation on the Agent page. See [known limitations](docs/progress/known-limitations.md).

## Quick start (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # fill in DF_BUSINESS_* and DF_SENDER_* at least
python -m dealerflow migrate      # creates state/dealerflow.db and seeds stages/templates
python -m dealerflow create-user you@example.com admin
python -m dealerflow demo         # optional: 8 SIMULATED fixture dealerships, no network
python -m dealerflow web          # http://127.0.0.1:8000
python -m dealerflow worker       # second terminal: agent ticks, sending, inbox polling
python -m dealerflow backup ./backups   # verified backup; `restore FILE --yes` to restore
pytest                            # 136 tests, no network
```

Docker: `docker compose up --build` runs `migrate`, then `web` and `worker`.

## Documents

| Document | What it covers |
|---|---|
| [docs/architecture/shared-stack.md](docs/architecture/shared-stack.md) | Agreed stack, shared models, API contract and provider interfaces for all phases |
| [docs/architecture/integration-matrix.md](docs/architecture/integration-matrix.md) | Every provider: purpose, auth, scopes, status, tested how |
| [docs/architecture/integration-setup.md](docs/architecture/integration-setup.md) | Step-by-step setup for Gmail/SMTP, Google Calendar, Zoom, Twilio, Places |
| [docs/architecture/environment.md](docs/architecture/environment.md) | Every environment variable |
| [docs/architecture/autonomous-agent.md](docs/architecture/autonomous-agent.md) | Agent states, tick steps, safety gates, how to go live |
| [docs/research/feasibility-report.md](docs/research/feasibility-report.md) | Data sources, providers, law: what's allowed |
| [docs/research/source-register.md](docs/research/source-register.md) | Sources, access dates, confidence |
| [docs/audit/launch-readiness-audit.md](docs/audit/launch-readiness-audit.md) | **Phase 1.5 audit: verdict, blockers, priorities** (plus deployment, security, integration, compliance, lead-sourcing and pilot docs in `docs/audit/`) |
| [docs/progress/build-status.md](docs/progress/build-status.md) | What's implemented and tested |
| [docs/progress/known-limitations.md](docs/progress/known-limitations.md) | What's missing, untested or needs credentials |

This is not legal advice. Have a lawyer review your outreach program before going live.
