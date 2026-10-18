# Car Dealer Future

AI-assisted marketing and operations platform for independent used-car dealerships (initial market: Sugar Land /
Houston, TX). One FastAPI application and one database for all four project phases.

- **Phase 2 (implemented):** dealership inventory ingestion, validation and sync; bilingual (English/Spanish)
  listing generation with guardrails; review and approval workspace; publishing channels (Facebook Marketplace manual
  workflow, Meta vehicle catalog API, CSV export); per-dealership reporting.
- **Outreach agent (implemented, sandbox-verified):** persistent agent that discovers and qualifies dealership
  prospects, drafts evidence-based emails, enforces consent/suppression/limits, classifies replies and books meetings.
- Shared foundation: auth and roles, tenant isolation, audit log, encrypted credentials, durable job queue + worker.

## Quick start
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # set APP_ENCRYPTION_KEY (see the comment in the file)
alembic upgrade head
SEED_ADMIN_PASSWORD=change-me-admin SEED_DEALER_PASSWORD=change-me-dealer python -m scripts.seed_fixtures
uvicorn app.main:app --reload     # http://localhost:8000  (API docs: /api/docs)
python -m app.worker              # in a second terminal
python -m pytest
```
The seed data is clearly labelled fixture data ("DEMO - Fixture Motors (test data)"). Nothing publishes listings or
contacts anyone unless live integrations are configured **and** explicitly authorised.

## Documentation
- `docs/architecture/stack-and-interfaces.md`: agreed stack and shared interfaces for all phases (read first)
- `docs/architecture/integration-matrix.md`: what each integration does and its verified status
- `docs/progress/build-status.md`: implemented features and test results
- `docs/progress/known-limitations.md`: blockers, missing credentials, unfinished work
- `docs/operations/deployment.md`: running, migrations, monitoring, runbooks
- `docs/research/`: Meta/inventory and outreach/compliance research briefs + source register
