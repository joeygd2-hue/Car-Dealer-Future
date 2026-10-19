# Car-Dealer-Future: dealerflow

Prospecting, CRM, outreach and a persistent outreach agent, for a business that sells marketing
automation to independent used-car dealerships around Sugar Land and Houston, Texas.

**Phase 2 (also in this code):** dealership clients and users with strict per-dealership isolation; inventory
import (CSV, spreadsheet, DMS export, authorised feeds, manual entry) with validation and sync history; English and
Spanish listings generated only from verified data, with guardrails, review, approval and revision history; publishing
via a manual Facebook Marketplace workflow (no API exists for it), the Meta vehicle catalog (guarded) and CSV export;
per-dealership reports. Open **Dealerships** in the app.

**Phase 1 (this code):** lead discovery and import, deduplication, explainable scoring, CRM pipeline,
human-approved email outreach (English/Spanish), reply import and classification, opt-out and
suppression enforcement, Zoom and calendar scheduling, click-to-call, daily and weekly reports, and
a persistent autonomous agent with pause and emergency stop.

**Phase 4 (also in this code):** a scoped client portal, finance (invoices, payments,
credits, expenses with receipts, agreements, MRR/ARR, receivables, CAC, churn, cash flow, reconciliation, CSV
export), AI/integration usage costs with budgets and alerts, a configurable service catalog, onboarding
checklist with automation gating, support tickets, an authorized-site SEO audit service, monthly client
reports, and a production-readiness security review. Client email is also SIMULATED until enabled and
authorized per client (`DF_CLIENT_EMAIL_SENDING_ENABLED`).

**Not yet in this code:** Phase 3 (customer-response engine) is still a separate app on
`claude/magical-darwin-nncp64`; buyer conversations here are interim SIMULATED stand-ins. Build status and how the
phases were integrated: [build status](docs/progress/build-status.md). Plan for individual sellers (MyBot Flipper):
[flipper plan](docs/product/flipper-plan.md).

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
python -m dealerflow demo-dealer  # optional: a SIMULATED dealership client with fixture inventory (Phase 2)
python -m dealerflow web          # http://127.0.0.1:8000
python -m dealerflow worker       # second terminal: agent ticks, sending, inbox polling
python -m dealerflow backup ./backups   # verified backup; `restore FILE --yes` to restore
pytest                            # 260 tests, no network
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
| [docs/architecture/system-design.md](docs/architecture/system-design.md) | Processes, tenancy and roles, Phase 4 modules, data model |
| [docs/security/threat-model.md](docs/security/threat-model.md) | Security review: controls, issues fixed, remaining risks |
| [docs/operations/deployment.md](docs/operations/deployment.md) | Deploying web + worker, going live in order, production checklist |
| [docs/operations/runbook.md](docs/operations/runbook.md) | Monitoring, incidents, backups/restore, retention, deletion |
| [docs/business/service-packages.md](docs/business/service-packages.md) | Service catalog and client agreements (prices are proposals) |
| [docs/business/metrics-and-reporting.md](docs/business/metrics-and-reporting.md) | How every finance, cost and client-report metric is defined |
| [docs/progress/build-status.md](docs/progress/build-status.md) | What's implemented and tested |
| [docs/progress/known-limitations.md](docs/progress/known-limitations.md) | What's missing, untested or needs credentials |

This is not legal advice. Have a lawyer review your outreach program before going live.
