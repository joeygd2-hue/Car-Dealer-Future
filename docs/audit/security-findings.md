# Security findings (Phase 1.5 audit, 2026-10-18)

Scope: the `dealerflow` application on branch `claude/charming-ritchie-358e1g`, its Docker image,
and its data handling. Method: code review, adversarial tests (`tests/test_adversarial.py`,
`tests/test_web.py`), a container run, a log leak scan, and `pip-audit`. No external penetration
test was done.

Severity: **High** = production blocker. **Medium** = fix before scaling past the pilot. **Low** = accept or schedule.

## Fixed during this audit

| ID | Finding | Severity | Fix | Test |
|---|---|---|---|---|
| F1 | Emergency stop blocked only email sends; bookings, inbox polling, research, calls and discovery still reached providers | High | Central gate in `audit.track_integration`; jobs defer without using attempts | `test_emergency_stop_blocks_every_external_path`, `…_call_and_discovery_buttons` |
| F2 | Revoked credentials were retried indefinitely by each job | Medium | Auth failure disables that provider until an admin's **Verify credentials** check passes | `test_auth_failure_disables_provider_until_reverified` |
| F3 | Provider failures inside failed jobs were lost on rollback (gap in the audit trail) | Medium | Failures and blocked attempts are replayed after rollback | `test_failed_provider_call_inside_job_stays_logged` |
| F4 | JSON API accepted cookie-authenticated writes without a CSRF token | Medium | `check_csrf` on the API router (Bearer API keys exempt) | `test_api_cookie_writes_need_csrf_…` |
| F5 | SSRF: website research would fetch internal addresses (metadata service, localhost, RFC 1918) supplied by an imported list | High | `assert_public_url` on every hop, including robots.txt and redirects | `test_website_research_refuses_private_addresses`, `test_redirect_to_private_address_is_refused` |
| F6 | Unsubscribe tokens (which contain the recipient's email, signed but readable) were written to request logs | Low | Logged as `/u/[token]` | `test_unsubscribe_tokens_are_not_logged` |
| F7 | A password change did not end existing sessions | Medium | Session bound to a password-hash fingerprint | `test_password_change_ends_existing_sessions` |
| F8 | No backup/restore | High | `backup` / `restore` commands (SQLite online backup with integrity check; pg_dump/pg_restore) | `test_sqlite_backup_and_restore`; Postgres and container runs below |
| F9 | No data retention control | Medium | `retention` command | `test_retention_redacts_old_bodies_but_keeps_suppressions` |
| F10 | Missing CSP/HSTS/no-store headers | Low | Added (HSTS in production only) | header check in container run |
| F11 | A real provider could email SIMULATED fixture prospects or reserved test domains | Medium | Compliance blocker | `test_real_provider_never_contacts_simulated_or_reserved_addresses` |
| F12 | Lost Zoom response on retry could create a duplicate meeting | Medium | Attempt checkpoint + lookup before re-creating | `test_lost_zoom_response_does_not_create_a_second_meeting` |
| F13 | 83 stale build artifacts committed to the repo (`build/lib`) | Low | Removed; `build/` ignored | — |

## Open findings

| ID | Finding | Severity | Plan | Status |
|---|---|---|---|---|
| **S1** | **Secrets are not encrypted at rest.** They come from environment variables or a `.env` file in plain text on the host | **High: production blocker** | See the mitigation plan below | Partially mitigated (file-based secrets supported) |
| S2 | No multi-factor authentication | Medium | Put the app behind an identity-aware proxy with MFA (Cloudflare Access, Google IAP, Tailscale) for the pilot; native TOTP later | Open |
| S3 | Login rate limit is in-memory, per process; it resets on restart | Low | Acceptable with one web process behind an MFA proxy; move to a DB-backed counter if exposed publicly | Open |
| S4 | Sessions are client-side signed cookies; logout can't revoke a copied cookie before expiry (12 h) | Low | Mitigated by F7 and user deactivation (checked every request); add a server-side session table if needed | Open |
| S5 | API keys never expire | Low | Rotate by regenerating; add expiry later | Open |
| S6 | Base image `python:3.12-slim` isn't pinned by digest; no image vulnerability scan was run (no scanner available) | Medium | Pin the digest; scan with Trivy or Grype in CI | Open |
| S7 | No TLS in the app; relies on a reverse proxy. Secure cookies need HTTPS | Medium | Deploy behind Caddy or nginx with TLS; HSTS is already sent in production | Open (deployment) |
| S8 | The database file and backups hold prospect PII and message bodies, unencrypted | Medium | Encrypted disk or volume, and encrypted off-host backups (e.g. `restic` with a separate key) | Open |
| S9 | Audit log is append-only by convention, not by mechanism (a DB admin could alter it) | Low | Periodic export of the audit log to write-once storage | Open |
| S10 | Untrusted `.xls`/`.xlsx` parsing (xlrd/openpyxl) by rep-level users could be used for denial of service | Low | 20 MB upload cap; uploads restricted to logged-in reps; acceptable | Accepted |

`pip-audit` on the pinned dependency set: **no known vulnerabilities** (2026-10-18).
Container log scan for the secret key: **0 occurrences**.

## S1 mitigation plan: secrets at rest (required before production)

**Current state.** Credentials (SMTP/IMAP passwords, Google client secret and refresh token, Zoom client
secret, Twilio token, `DF_SECRET_KEY`) are read from the environment. They are never written to the database,
logs, pages or backups. As of this audit, each can also be read from a file in `DF_SECRETS_DIR`
(default `/run/secrets`), named like the variable in lower case, e.g. `/run/secrets/df_smtp_password`.

**Plan (in order):**

1. **Before the pilot (required):**
   - Don't put secrets in `.env`. Use Docker Compose `secrets:` (mounted on tmpfs at `/run/secrets`) or the host's secret manager, injected at start.
   - Make the host disk encrypted (LUKS, or the cloud provider's default volume encryption).
   - Restrict the host to one admin user with SSH keys.
   - Rotate `DF_SECRET_KEY` only with a plan: it invalidates sessions *and* already-sent unsubscribe links (CAN-SPAM requires those to work for 30 days).
2. **Least privilege:**
   - Gmail: an Internal OAuth app with only `gmail.send` + `gmail.readonly`.
   - Calendar: `calendar.freebusy` + `calendar.events`.
   - Zoom: S2S app with only the meeting scopes for one host user.
   - Twilio: a restricted API key, not the master auth token.
3. **Revocation runbook:** if the host is compromised, revoke the Google OAuth grant (myaccount.google.com → Security → Third-party access), deactivate the Zoom app, roll the Twilio key and mailbox app password, then rotate `DF_SECRET_KEY`. Since F2, the app stops using a provider once it sees an auth failure.
4. **Later (if the app grows to several hosts):** move secrets to a managed store (AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault) fetched at start-up, and store OAuth refresh tokens encrypted with a KMS-managed key if they ever need to live in the database.

Until step 1 is done, S1 remains a **production blocker**.
