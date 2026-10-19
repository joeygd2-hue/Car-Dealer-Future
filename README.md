# Car-Dealer-Future: dealerflow

One FastAPI application for a business that sells marketing automation to independent used-car dealerships around
Sugar Land and Houston, Texas. Phases 1, 2, 3 and 4 run here together (one login, database, job queue, outreach
agent and audit log).

**Phase 1:** lead discovery and import, deduplication, explainable scoring, CRM pipeline,
human-approved email outreach (English/Spanish), reply import and classification, opt-out and
suppression enforcement, Zoom and calendar scheduling, click-to-call, daily and weekly reports, and
a persistent autonomous agent with pause and emergency stop.

**Phase 2:** per-dealership inventory import (CSV, spreadsheet, DMS export, authorised feeds, manual entry) with
validation and sync history; English and Spanish listings generated only from verified data, with guardrails, review,
approval and revision history; publishing via a manual Facebook Marketplace workflow (no API exists for it), the Meta
vehicle catalog (guarded) and CSV export; per-dealership reports. Staff open **Inventory & listings**; dealership users
get their own workspace.

**Phase 3:** car-buyer conversations per dealership: an embeddable website chat, Messenger and Twilio SMS webhooks,
ADF/XML lead intake and pasted Marketplace messages land in one inbox; a grounded English/Spanish responder drafts
replies only from verified inventory and dealer-approved information, escalates everything else with a summary, and
records test-drive requests for staff to confirm. Automated replies (Modes B/C) need a passing evaluation and the
dealership admin's authorization. **Every customer channel is a sandbox in this build: no reply reaches a customer.**

**Phase 4:** dealership clients created from won prospects, with onboarding checklist and automation gating, a scoped
client portal, finance (invoices, payments, credits, expenses with receipts, agreements, MRR/ARR, receivables, CAC,
churn, cash flow, reconciliation, CSV export), AI/integration usage costs with budgets and alerts, a configurable
service catalog, support tickets, an authorized-site SEO audit service and monthly client reports built from Phase 2
data. Client email is SIMULATED until enabled and authorized per client (`DF_CLIENT_EMAIL_SENDING_ENABLED`).

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
python -m dealerflow demo-dealer  # optional: a SIMULATED dealership client with fixture inventory (Phase 2);
                                  # its website chat test page is linked from the dealership's Customer replies tab
python -m dealerflow web          # http://127.0.0.1:8000
python -m dealerflow worker       # second terminal: agent ticks, sending, inbox polling
python -m dealerflow backup ./backups   # verified backup; `restore FILE --yes` to restore
pytest                            # 268 tests, no network (DF_TEST_PG_URL=... to run on Postgres)
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
