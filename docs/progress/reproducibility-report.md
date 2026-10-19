# Reproducibility report

_2026-10-18. Fresh `git clone` of `claude/magical-darwin-nncp64` into an empty directory, new virtualenv, the README steps
followed literally, settings taken only from the copied `.env` (shell variables unset)._

## Procedure
```bash
git clone --branch claude/magical-darwin-nncp64 <repo> clean && cd clean
uv sync --frozen
cp .env.example .env            # then set ENCRYPTION_KEY (and BASE_URL for the port used)
uv run alembic upgrade head && uv run alembic check
uv run python -m scripts.seed_demo
uv run ruff check . && uv run pytest -q
uv run uvicorn app.main:app --port 8000      # process 1
uv run python -m app.worker                   # process 2 (killed with SIGKILL mid-run, then restarted)
SMOKE_BASE_URL=http://127.0.0.1:8000 uv run python -m scripts.smoke_e2e
```

## Defects found by the clean run (and fixed)
| # | Defect | Impact | Fix | Regression test |
|---|---|---|---|---|
| R1 | `.env.example` shipped `REQUIRE_ADMIN_MFA=` (blank); pydantic rejected `''` as a boolean | `alembic upgrade`, seed, app and **121 tests** failed for anyone following the README | blank `KEY=` lines fall back to defaults; example no longer ships a blank boolean | `test_env_example_loads_as_documented`, `test_blank_values_fall_back_to_defaults` |
| R2 | Tests read the developer's `.env` | a local `.env` could break or alter the suite | tests set `APP_ENV_FILE` to a non-existent file (hermetic) | covered by R1 tests |
| R3 | `alembic upgrade head` failed on a fresh checkout (`./data/` missing) | migrations impossible without manual `mkdir` | `migrations/env.py` creates the SQLite directory | `test_upgrade_creates_missing_sqlite_directory` |
| R4 | `seed_demo` silently created the schema with `create_all()` when migrations hadn't run | unversioned database; later `alembic upgrade` would fail | seed refuses an unmigrated database | `test_seed_refuses_unmigrated_database` |

| R5 | **Production race surfaced by an intermittent test failure** (≈1 in 50–150 runs under load): a second job for the same message saw `SENDING` and declared the owner crashed → `UNCERTAIN`, even while the owner was mid-send or had just finished, overwriting a successful outcome. No duplicate was ever sent, but a healthy send was falsely flagged and its final status depended on timing. Root cause: the two reads ("row is SENDING", "is there an in-flight attempt?") are not one atomic snapshot (SQLite legacy-transaction reads; PostgreSQL READ COMMITTED behaves the same). Affected customer, outreach and notification senders. | false "delivery uncertain" alerts; lost success status | (1) a `SENDING` row with a *young* in-flight attempt defers; (2) a `SENDING` row with *no* in-flight attempt is re-checked, never overwritten; (3) the crash takeover is a pair of conditional UPDATEs (attempt still `IN_FLIGHT` **and** row still `SENDING`) inside a savepoint | `test_duplicate_job_does_not_mark_a_live_send_uncertain`; deterministic interleaving `test_owner_finishing_between_checks_is_not_overwritten[customer|outreach]` — **all verified failing on the previous code** |
| R6 | Two race tests could fail spuriously under SQLite write-lock contention (a thread that hit "database is locked" was dropped/gave up) | flaky CI | tests retry like the real worker loop and report thread failures explicitly; assertions unchanged | the tests themselves |

R1–R4 were invisible in the development environment because it had no `.env` and an existing `data/` directory.
R5 only appears under timing contention, which is why it took repeated runs to catch.

## Results after fixes
| Check | Result |
|---|---|
| `uv sync --frozen` | OK (lockfile honoured) |
| Fresh migration `4122abfb2c23 → 44a34c76e0bf`, `alembic check` | OK, "No new upgrade operations detected" |
| Seed | OK |
| `ruff check .` | clean |
| `pytest` | 184 passed after R5/R6; stress: all 10 worker-resilience tests × 150 consecutive runs, **0 failures** (before the fix: 1 failure in 54) |
| Separate web + worker processes, `smoke_e2e.py` | **32/32 checks passed**, including a `kill -9` of the worker mid-run and a replacement worker; 0 tracebacks in web/worker logs |

### What the 32 smoke checks verify
- **Authentication & roles**: dealer staff get 403 on `/admin` and `/admin/pilot`; anonymous users are redirected to login; a dealer manager gets 404 for another client created during the run.
- **Lead intake & staff response**: 6 website-chat leads (EN, ES, financing, test drive, STOP, stale vehicle) → worker drafts → staff send through the portal.
- **Inventory accuracy**: verified price quoted from inventory; a vehicle last verified 10 days ago has its price withheld and the question escalated (`uncertain_inventory`); financing escalated with no terms invented; test-drive time recorded but not promised; STOP recorded with no reply.
- **Sandbox-only delivery**: staff reply and appointment confirmation are `SIMULATED`; the website visitor receives nothing; the send ledger has **zero live attempts**.
- **Worker**: separate-process heartbeat healthy at start and end; SIGKILL + replacement mid-run without losing work.
- **Emergency stop**: banner visible to dealer staff; a staff send during the stop is `BLOCKED` and logged; lifted with a reason.
- **Audit**: HMAC integrity check passes over all rows.

## Not reproducible in this environment
Docker (no image build here), PostgreSQL (not installed), real provider accounts (none authorized).
