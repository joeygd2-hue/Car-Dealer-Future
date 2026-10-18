# Hardened controls on the unified branch

Branch `claude/mybot-unification` (Phase 2 foundation + Phase 1.5 hardening). Every control from the
launch-readiness audit is present and covered by passing tests here. Controls that Phase 2 code paths could bypass
were fixed and given new regression tests (marked **new**).

| Control | Where it lives | Regression tests (all passing) |
|---|---|---|
| Global emergency stop: every provider call | `audit.track_integration` → `_gate` | `test_adversarial::test_emergency_stop_blocks_every_external_path`, `…_call_and_discovery_buttons`, `test_agent::test_emergency_stop_halts_sends_and_returns_queue_to_draft`; **new** `test_unification_controls::test_emergency_stop_blocks_listing_publication`, `…_blocks_ai_listing_copy_and_falls_back_to_template` (both verified to fail with the check disabled) |
| Campaign pause and send-time authorization | `services.outreach._campaign_hold`, `live_sending_allowed` | `test_paused_campaign_holds_already_queued_messages`, `test_agent_approved_messages_wait_when_agent_paused_or_out_of_hours`, `test_real_provider_never_auto_sends_without_live_authorization` |
| Duplicate-send prevention | idempotent job keys, status checks, `sending` state | `test_duplicate_send_jobs_send_once`, `test_interrupted_send_is_not_blindly_resent`; P2 `test_api_channel_submit_confirm_and_idempotency` |
| Duplicate-booking prevention | deterministic calendar IDs, Zoom attempt checkpoint + lookup | `test_lost_zoom_response_does_not_create_a_second_meeting`, `test_calendar_failure_retry_reuses_zoom_meeting` |
| Provider failure logging and uncertain delivery | `_record_failure` / `replay_failures`; `sending` → failed "outcome unknown" | `test_failed_provider_call_inside_job_stays_logged`, `test_every_external_state_change_has_an_audit_entry`; **new** `test_ai_call_is_logged_through_the_gate` |
| Auth failure disables the provider | `set_integration_disabled` | `test_auth_failure_disables_provider_until_reverified` |
| Sessions: expiry, password-bound invalidation | `web.deps.current_user` (`pv`) | `test_password_change_ends_existing_sessions`; **new** `test_password_change_ends_dealer_user_sessions` |
| CSRF | `web.deps.check_csrf` | `test_csrf_required_for_form_posts`, `test_api_cookie_writes_need_csrf_…`; **new** `test_csrf_covers_every_unsafe_method[POST/PUT/PATCH/DELETE]` (was POST-only) |
| Roles and tenant confinement | `require()`, `_restrict_dealer_user`, `tenancy` | `test_viewer_and_rep_cannot_do_privileged_things`, P2 `test_dealer_isolation.py` (6 tests); **new** `test_dealer_user_still_confined_to_own_workspace` |
| Public-IP-only fetching, redirects re-checked | **one** guard: `services.netguard` (website research delegates to it) | `test_website_research_refuses_private_addresses[7 cases]`, `test_redirect_to_private_address_is_refused`, `test_robots_unreachable_means_disallow`; **new** `test_shared_ssrf_guard_*` (CGNAT, DNS fail-closed, https for feeds), `test_feed_fetch_uses_the_shared_guard` |
| Secret handling | env or files in `DF_SECRETS_DIR` | **new** `test_secrets_can_come_from_files_not_the_environment` (found and fixed: the directory was read only at import time) |
| Privacy: no tokens/emails in logs; retention | `web.app` log redaction; `retention.apply` | `test_unsubscribe_tokens_are_not_logged`, `test_retention_redacts_old_bodies_but_keeps_suppressions` |
| Backups and restore validation | `backup.py` | `test_sqlite_backup_and_restore` (Postgres verified by hand in the audit) |
| Worker recovery | `jobs.run_pending` stale-lock release; `agent.recover_after_restart` | `test_stale_running_job_is_recovered`, `test_recovery_marks_interrupted_runs` |
| Opt-out and suppression | `compliance.email_blockers` at draft, approve and send; `inbox.apply_consequences` | `test_opt_out_reply_suppresses_and_stops_everything`, `test_spanish_opt_out`, `test_send_time_recheck_blocks_opt_out_after_approval`, `test_suppressed_domain_blocks_draft_approval`, `test_mailer_daemon_bounce_is_matched_and_suppressed`, classifier opt-out cases |

## Fixes made on this branch that the hardened source branch doesn't have

| Fix | Why |
|---|---|
| AI listing generation goes through `track_integration` | Phase 2 called Anthropic directly; an emergency stop or disabled provider did not stop it |
| Single SSRF guard (`netguard`) with the strictest rules | Two guards existed; Phase 2's allowed CGNAT ranges and treated DNS failure as allowed |
| CSRF on PUT/PATCH/DELETE | Phase 4 brings PATCH routes |
| `DF_SECRETS_DIR` resolved when settings are built | Was resolved at import, so setting it later had no effect |

The hardened branch `claude/charming-ritchie-358e1g` was left unchanged, as instructed. The secrets-dir fix above also
applies to it; the unified branch supersedes it.
