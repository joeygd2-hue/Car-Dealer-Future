# Threat model and security review

Scope: the whole `dealerflow` application as of Phase 4 (branch `claude/blissful-davinci-82py6w`). Reviewed
2026-10-18. This is an engineering review, not a penetration test.

## Assets

Prospect business data and evidence; client dealerships' data (inventory, car-buyer conversations and contact
details, invoices, SEO findings, reports); internal finance; provider credentials (environment only);
outbound sending reputation; the ability to send email or book meetings in the business's name.

## Actors and trust boundaries

| Actor | Boundary | Main threats |
|---|---|---|
| Anonymous internet | `/login`, `/u/{token}`, `/webhooks/*`, `/healthz`, `/static` | Credential stuffing, forged webhooks, unsubscribe-token abuse, DoS |
| Dealership user | Own workspace (`/dealers/{id}`) and portal only | Reading another dealership's data (IDOR), reaching staff pages, escalating role |
| Staff (viewer → admin) | Role checks | Over-privileged actions, accidental live sending, data export |
| Providers / webhooks | Signed requests | Spoofed or replayed events, duplicate deliveries |
| Crawled/researched websites | Untrusted HTML | Parser abuse, SSRF via redirects, prompt injection if AI is enabled |
| Worker | Same DB | Crash mid-job, duplicate execution, runaway retries |

## Controls (verified by tests unless marked)

| Area | Control | Test |
|---|---|---|
| Authentication | PBKDF2 password hashes (390k iterations); signed session cookie (HttpOnly, SameSite=Lax, Secure in production, 12 h max age); per-user API keys stored as SHA-256; failed-login limiter (5 / 15 min per email+IP); per-IP API rate limit (default 300/min) | `test_web.py`, `test_phase4_security.py::test_api_rate_limit` |
| Authorization | Ordered staff roles; dealership users (`client_id` + `client_role`) confined centrally in `current_user` to their workspace and the portal, and never admitted by `require()`; settings/CLI cannot create or convert dealership users | `test_client_users_are_confined_to_the_portal` (14 paths), `test_dealership_home_is_their_own_workspace`, Phase 2 `test_dealer_isolation.py`, `test_role_matrix_*`, `test_staff_user_creation_cannot_make_unbound_client_users`, `test_settings_cannot_convert_a_portal_user_to_staff`, `test_admin_resets_portal_password_only_within_that_client` |
| Client isolation | Portal: `services/clients.scope()` / `owned_or_404()` on every query; workspace: Phase 2 tenant session + composite FKs; 404 for out-of-scope ids; drafts never visible; portal tickets bound to the user's client; staff routes check the URL client owns the row | `test_portal_lists_and_direct_ids_are_isolated`, `test_drafts_never_visible_in_portal`, e2e step 14, `test_lead_summary_reaches_the_correct_dealership` |
| CSRF | Token on every cookie-authenticated POST (form field or `X-CSRF-Token`); Bearer API calls exempt | `test_csrf_required_for_form_posts`, `test_cookie_json_post_needs_csrf_header` |
| Redirects | `safe_next()` blocks `//host` and `/\host`; dealership users land in their workspace, and `next` is honoured only inside it or the portal | `test_open_redirect_variants_blocked`, `test_client_users_cannot_log_in_to_staff_pages_via_next` |
| Browser hardening | CSP (`script-src 'self'`, `frame-ancestors 'none'`, `form-action 'self'`, `object-src 'none'`), X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy, HSTS in production, `Cache-Control: no-store` on app pages; inline event handlers moved to `static/app.js` | `test_security_headers_and_no_inline_script` |
| Input validation | Pydantic schemas on the JSON API; DB check constraints on statuses, signs and amounts; money parsed to cents with ≤2 decimals; dates ISO | finance tests |
| Payment data | No card data stored: free-text fields rejected if they contain a Luhn-valid PAN; Stripe payloads reduced to an allow-list of fields | `test_card_numbers_rejected_and_redacted`, `test_stripe_invoice_paid_records_confirmed_payment_once` |
| Webhooks | Stripe `t=,v1=` HMAC-SHA256 with 5-minute tolerance, constant-time compare, disabled when no secret; Zoom and Twilio signatures (Phase 1); events stored once per id, processed by idempotent jobs | `test_stripe_webhook_signature_and_dedup`, `test_zoom_webhook_signature` |
| Uploads | Type allow-list + magic-byte check, 10 MB limit, random storage key, sanitized filename, path-traversal check on download, staff-only download | `test_upload_validation` |
| Secrets | Environment only (`SecretStr`); JSON log formatter redacts secret-named fields; Phase 4 `RedactingFilter`/`redact()` for free text; audit details scrubbed of secret-named keys | `test_audit_records_scrub_secrets`, `test_card_numbers_rejected_and_redacted` |
| Outbound safety | Mock providers by default; real outreach needs `DF_OUTREACH_SENDING_ENABLED` **and** admin live authorization; client email needs `DF_CLIENT_EMAIL_SENDING_ENABLED` **and** a per-client authorized connection; emergency stop | `test_agent.py`, `test_report_email_not_sent_with_real_provider_unless_authorized` |
| Crawling | robots.txt honoured, page cap, ≥0.5 s delay, stops on 429, GET/HEAD only, same-site only, no forms/logins | `test_crawler_stops_on_429_and_never_bypasses_robots`, `test_seo_never_writes_to_the_client_site` |
| Jobs | Atomic claim, idempotency keys, stale-lock recovery, bounded retries, failure hooks → alerts/tickets | `test_failures.py`, `test_audit_job_runs_through_worker_queue` |
| Production config | Startup refused with a weak `DF_SECRET_KEY`, or with non-https `DF_BASE_URL` when real email is enabled; warnings for SQLite/http | `test_production_refuses_insecure_config` |
| Errors | Generic 500 body; exception details only in logs | — |
| Dependencies | `pip-audit` reported no known vulnerabilities (2026-10-19, after the Phase 2 merge) | manual run |

## Issues found and fixed in Phase 4

1. Adding a client role would have exposed every staff page that used plain `Depends(current_user)` (23 routes) → central confinement in `current_user`.
2. Open redirect via `/\evil.example` in the login `next` parameter → fixed (also fixed independently in Phase 1's `safe_next`).
3. Settings and CLI could create a `client` user with no client → restricted to staff roles.
4. Inline `onsubmit`/`onclick` handlers prevented a strict CSP → moved to `static/app.js`; CSP added.
5. No API rate limit, cache headers or production configuration validation → added.
6. A foreign-key cycle (clients ↔ prospects ↔ users) made schema ordering undefined → resolved; after the Phase 2 merge `users.client_id` comes from Phase 2's migration.
7. The Settings user form could convert an existing dealership user into staff (keeping `client_id`) → refused; dealership users are created in the dealership's settings and reset/deactivated only from their client's page.
8. (Phase 2 merge) Two client-creation paths (`/dealers`, `/clients`) behaved differently: `/dealers` skipped the onboarding checklist and did not suppress the won prospect's address → both now use `services/clients.create_client`; new clients start in `onboarding`, so scheduled feed sync waits for activation.

## Phase 3 (customer conversations) controls

Added on `claude/mybot-phase3-integration`; all verified by automated tests, the workflow also in a headless browser.

| Area | Control | Test |
|---|---|---|
| Dealership isolation | Every conversation table carries `client_id` with composite `(client_id, id)` foreign keys; routes use `dealer_ctx` (tenant-scoped session); another dealership's ids → 404 (pages and API); drafts/messages cannot be used across conversations; assignment only to the dealership's own users; public intake resolves the dealership from its key and only links that dealership's vehicles | `test_conv_security.py`, `test_conv_migration.py` (DB refuses a cross-dealership row) |
| Server-side roles | `view` / `converse` / `manage` permissions; viewers read only; reviewers reply; only admins configure automation | `test_roles_are_enforced_on_the_server`, `test_viewer_cannot_confirm`, `test_dealer_pages_render_for_each_role` |
| AI drafts | Grounded composer uses only verified facts; output validator; drafts are labelled "DRAFT, not sent"; staff text with unsupported claims needs an audited acknowledgement; optional Claude output discarded if it adds facts | `test_conv_pipeline.py`, `test_staff_text_with_unsupported_claims_needs_acknowledgement`, `test_ai_rephrasing_*` |
| Automatic sending | Off by default; needs evaluation + written authorization + onboarding gate + approved templates + hours + limits; facts re-validated at send time | `test_auto_send_requires_every_gate`, `test_auto_send_revalidates_facts_at_send_time` |
| Opt-outs | EN/ES opt-out phrases → conversation/contact flagged, suppression row, drafts superseded, queued messages blocked; suppressions survive retention purges | `test_opt_out_stops_everything`, `test_opted_out_customer_gets_no_appointment_messages` |
| Emergency stop | Blocks every customer send at send time (the draft returns to review) and every provider call through `track_integration` | `test_emergency_stop_blocks_queued_replies`, browser walkthrough |
| Duplicates / crashes | Inbound dedupe per client+channel+provider id; idempotent jobs; double-submit keys; atomic `queued→sending` claim committed before delivery; a stale `sending` becomes `uncertain` (never auto-resent) and staff reconcile | `test_double_submit_and_duplicate_jobs_send_once`, `test_crashed_send_becomes_uncertain_and_is_never_resent_automatically`, live `kill -9` worker check |
| Live delivery | Website chat replies are SIMULATED unless `DF_CUSTOMER_MESSAGING_LIVE_ENABLED` and a connected, authorized `website_chat` connection; the widget only shows `sent` replies | `test_live_website_chat_needs_env_switch_and_connected_channel` |
| Public endpoints | DB rate limits shared across processes; size limits; CORS to the dealership's site only; ADF parsed with defusedxml (DTD/entities refused); secret ADF URL | `test_public_chat_is_rate_limited_across_processes`, `test_adf_intake_creates_a_lead_once_and_refuses_hostile_xml`, `test_cors_only_for_the_dealership_site` |
| Audit | Conversation events record ids, statuses and reasons only — no message text or contact details | `test_audit_trail_has_ids_but_no_customer_text` |
| Data lifecycle | Retention purge clears names, e-mails, phones and message text after `DF_RETENTION_DAYS_CONVERSATIONS`; export includes every client-owned table; deletion removes tenant rows children-first and anonymises the dealership's users | `test_conv_data_lifecycle.py` |

## Security improvements made during the Phase 3 integration

1. **MFA** (was: none). TOTP (stdlib, ported from Phase 3) with encrypted secrets, single-use hashed recovery codes and
   replay protection; second login step; enforced in production for staff admins/managers and dealership admins
   (`DF_MFA_ENFORCED` overrides); enabling/disabling MFA ends other sessions. API keys are not covered (documented).
   Tests: `test_mfa_*`.
2. **Rate limits across processes** (was: in-process only). Login lockout, MFA attempts, the per-IP API limit, public
   chat/ADF limits and auto-reply limits use database counters (`rate_counters`). Tests: `test_*_shared_between_processes`.
3. **Receipts encrypted at rest** (was: plaintext files). Fernet with `DF_ENCRYPTION_KEY` (required in production),
   SHA-256 integrity check on download, files written earlier stay readable. Test: `test_receipts_are_encrypted_at_rest`.
4. **SEO crawler SSRF** (found by Phase 3's review): redirects were followed automatically to any address → every hop is
   checked against private/loopback/link-local/reserved addresses. Test: `test_seo_crawler_refuses_internal_addresses_and_redirects`.
5. **Client deletion** failed with a foreign-key error once Phase 2/3 rows existed (tenant FKs do not cascade), blocking
   data-deletion requests → tenant rows are deleted children-first; the dealership's users are anonymised and can no
   longer authenticate. Test: `test_export_and_delete_a_dealership_with_conversations`.
6. **CSP blocked dealer vehicle photos** (Phase 2 shows photos from the dealer's image host; `img-src 'self'` refused
   them, found in the browser walkthrough) → `img-src 'self' data: https:`; scripts remain `'self'` only.

## Remaining risks (accepted or open)

| Risk | Status / mitigation |
|---|---|
| Sessions are signed cookies (no server-side revocation list) | Password or MFA changes end other sessions (fingerprint); logout clears the cookie; rotating `DF_SECRET_KEY` invalidates all sessions but also breaks sent unsubscribe links |
| API keys are not covered by MFA | Keys are per user and hashed; keep them in a secrets manager; rotate on exposure |
| `DF_ENCRYPTION_KEY` loss or leak | Loss makes MFA secrets and receipts unreadable (users re-enrol; receipts lost); leak exposes them. Keep it in the secrets store, back it up separately; key rotation is not automated |
| Database rate counters add a write per API request | Acceptable at pilot scale; a proxy/WAF limit is still recommended in front of the app |
| `style-src 'unsafe-inline'` | Phase 1 templates use inline `style=`; low risk with `script-src 'self'` |
| Prompt injection from customer messages or crawled pages | Drafts are deterministic; Claude rephrasing is off by default, only receives verified facts + the draft, and its output is validated |
| Car-buyer PII in conversations | Purged after `DF_RETENTION_DAYS_CONVERSATIONS` (default 365); no field-level encryption of message text |
| Audit log is not tamper-evident | Phase 3's HMAC-sealed audit log was not ported; restrict database access |
| `legacy_*` archive tables hold interim conversation PII | Not covered by the retention job; drop them with a reviewed migration after the converted data is checked |
| No external penetration test | Open |
