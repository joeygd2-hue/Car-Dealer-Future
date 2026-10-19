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
| Client user | Portal only | Reading another dealership's data (IDOR), reaching staff pages, escalating role |
| Staff (viewer → admin) | Role checks | Over-privileged actions, accidental live sending, data export |
| Providers / webhooks | Signed requests | Spoofed or replayed events, duplicate deliveries |
| Crawled/researched websites | Untrusted HTML | Parser abuse, SSRF via redirects, prompt injection if AI is enabled |
| Worker | Same DB | Crash mid-job, duplicate execution, runaway retries |

## Controls (verified by tests unless marked)

| Area | Control | Test |
|---|---|---|
| Authentication | PBKDF2 password hashes (390k iterations); signed session cookie (HttpOnly, SameSite=Lax, Secure in production, 12 h max age); per-user API keys stored as SHA-256; failed-login limiter (5 / 15 min per email+IP); per-IP API rate limit (default 300/min) | `test_web.py`, `test_phase4_security.py::test_api_rate_limit` |
| Authorization | Ordered staff roles; `client` role confined centrally in `current_user` to `/portal`, `/api/v1/portal` and its own `/dealers/{id}` workspace (dealership role `client_role` decides import/edit/approve/publish/manage there; missing role = denied); settings/CLI cannot create unbound client users | `test_client_users_are_confined_to_the_portal` (15 paths), `test_role_matrix_*`, `test_staff_user_creation_cannot_make_unbound_client_users`, `test_settings_cannot_convert_a_portal_user_to_staff`, `test_admin_resets_portal_password_only_within_that_client` |
| Client isolation | `services.tenancy.scope()` / `owned_or_404()` on every portal query; Phase 2 workspaces use tenant-scoped sessions (`tenancy.tenant_session`), refuse cross-client writes at flush, and composite `(client_id, id)` FKs (also for interim conversations → vehicles); 404 for out-of-scope ids; drafts never visible; portal tickets bound to the user's client; staff Phase 2/3 routes check the URL client owns the row | `test_portal_lists_and_direct_ids_are_isolated`, `test_drafts_never_visible_in_portal`, e2e step 14, `test_lead_summary_reaches_the_correct_dealership` |
| CSRF | Token on every cookie-authenticated POST (form field or `X-CSRF-Token`); Bearer API calls exempt | `test_csrf_required_for_form_posts`, `test_cookie_json_post_needs_csrf_header` |
| Redirects | `safe_next()` blocks `//host` and `/\host`; client users always land in `/portal` | `test_open_redirect_variants_blocked`, `test_client_users_cannot_log_in_to_staff_pages_via_next` |
| Browser hardening | CSP (`script-src 'self'`, `img-src 'self' data: https:` for dealer-hosted vehicle photos, `frame-ancestors 'none'`, `form-action 'self'`, `object-src 'none'`), X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy, HSTS in production, `Cache-Control: no-store` on app pages; inline event handlers moved to `static/app.js` | `test_security_headers_and_no_inline_script`, `test_csp_allows_dealer_photo_urls_but_no_inline_or_external_script` |
| Input validation | Pydantic schemas on the JSON API; DB check constraints on statuses, signs and amounts; money parsed to cents with ≤2 decimals; dates ISO | finance tests |
| Payment data | No card data stored: free-text fields rejected if they contain a Luhn-valid PAN; Stripe payloads reduced to an allow-list of fields | `test_card_numbers_rejected_and_redacted`, `test_stripe_invoice_paid_records_confirmed_payment_once` |
| Webhooks | Stripe `t=,v1=` HMAC-SHA256 with 5-minute tolerance, constant-time compare, disabled when no secret; Zoom and Twilio signatures (Phase 1); events stored once per id, processed by idempotent jobs | `test_stripe_webhook_signature_and_dedup`, `test_zoom_webhook_signature` |
| Uploads | Type allow-list + magic-byte check, 10 MB limit, random storage key, sanitized filename, path-traversal check on download, staff-only download | `test_upload_validation` |
| Secrets | Environment only (`SecretStr`); JSON log formatter redacts secret-named fields; Phase 4 `RedactingFilter`/`redact()` for free text; audit details scrubbed of secret-named keys | `test_audit_records_scrub_secrets`, `test_card_numbers_rejected_and_redacted` |
| Outbound safety | Mock providers by default; real outreach needs `DF_OUTREACH_SENDING_ENABLED` **and** admin live authorization; client email needs `DF_CLIENT_EMAIL_SENDING_ENABLED` **and** a per-client authorized connection; never to a SIMULATED client; emergency stop gates every provider call (outreach, client email, listing AI, Meta, feeds) | `test_agent.py`, `test_report_email_not_sent_with_real_provider_unless_authorized`, `test_emergency_stop_blocks_ai_and_client_email`, `test_simulated_dealership_never_gets_real_client_email` |
| Crawling and fetching | robots.txt honoured, page cap, ≥0.5 s delay, stops on 429, GET/HEAD only, same-site only, no forms/logins; one SSRF check (`integrations.website.assert_public_url`, public addresses only) for website research, inventory feeds and every SEO request and redirect hop | `test_crawler_stops_on_429_and_never_bypasses_robots`, `test_seo_never_writes_to_the_client_site`, `test_seo_crawler_refuses_internal_addresses`, `test_feed_urls_to_internal_addresses_are_rejected` |
| Jobs | Atomic claim, idempotency keys, stale-lock recovery, bounded retries, failure hooks → alerts/tickets | `test_failures.py`, `test_audit_job_runs_through_worker_queue` |
| Production config | Startup refused with a weak `DF_SECRET_KEY`, or with non-https `DF_BASE_URL` when real email is enabled; warnings for SQLite/http | `test_production_refuses_insecure_config` |
| Errors | Generic 500 body; exception details only in logs | — |
| Dependencies | `pip-audit` reported no known vulnerabilities (2026-10-18) | manual run |

## Issues found and fixed in Phase 4

1. Adding a client role would have exposed every staff page that used plain `Depends(current_user)` (23 routes) → central confinement in `current_user`.
2. Open redirect via `/\evil.example` in the login `next` parameter → fixed (also fixed independently in Phase 1's `safe_next`).
3. Settings and CLI could create a `client` user with no client → restricted to staff roles.
4. Inline `onsubmit`/`onclick` handlers prevented a strict CSP → moved to `static/app.js`; CSP added.
5. No API rate limit, cache headers or production configuration validation → added.
6. A foreign-key cycle (clients ↔ prospects ↔ users) made schema ordering undefined → `use_alter` on `users.client_id`.
7. The Settings user form could convert an existing client portal user into staff (keeping `client_id`) → refused; portal users are managed (password reset, deactivation) only from their client's page.

## Issues found and fixed during the Phase 1 + 2 + 4 integration (2026-10-19)

1. The SEO crawler fetched staff-entered URLs and followed redirects with no SSRF check → now checks the start URL
   and every redirect hop against the shared public-address guard.
2. Listing AI calls bypassed the emergency-stop gate → routed through `track_integration` like every provider call.
3. Client email could reach a SIMULATED (demo) dealership if a connection was marked connected → refused.
4. Two SSRF guards with different rules (one let unresolvable hosts through without the `is_global` check) → one
   core check, wrapped for inventory feeds.
5. The CSP inherited from Phases 1/4 blocked dealer-hosted vehicle photos in Phase 2 review pages (found in the
   browser pass) → `img-src https:` added; scripts unchanged.
6. Client-attributed delivery costs (e.g. AI tokens for a client) would have inflated customer-acquisition cost →
   excluded from CAC.

## Remaining risks (accepted or open)

| Risk | Status / mitigation |
|---|---|
| Rate limiters are in-process | Fine for one web process; use a shared store (e.g. Redis) or proxy limits before scaling out |
| Sessions are signed cookies (no server-side revocation list) | Logout clears the cookie; rotating `DF_SECRET_KEY` invalidates all sessions but also breaks sent unsubscribe links |
| No MFA | Open. Recommend MFA via an identity-aware proxy or SSO before many staff accounts exist |
| `img-src https:` | A dealer photo host learns the viewer's IP when a photo is shown; page URLs are not sent (same-origin referrer). Proxying images would remove this |
| `style-src 'unsafe-inline'` | Phase 1 templates use inline `style=`; low risk with `script-src 'self'` |
| Attachments on local disk, unencrypted | Use an encrypted volume or object storage with encryption at rest |
| Prompt injection from crawled pages | AI is off by default and never sends; AI output always needs approval |
| Car-buyer PII in conversations | Purged after `DF_RETENTION_DAYS_CONVERSATIONS` (default 365); no field-level encryption |
| No external penetration test | Open |
