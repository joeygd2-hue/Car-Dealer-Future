# Known limitations (Phase 1 + 1.5 audit)

Last updated 2026-10-18 (after the launch-readiness audit; see `docs/audit/launch-readiness-audit.md` for
blockers and priorities). Be explicit with anyone relying on this system: **no real email, calendar, Zoom,
Twilio, Google Places or website request has been made by this code yet.** Every end-to-end test ran against
SIMULATED providers or mocked HTTP. The one real external fetch was a manual download of the public TxDMV dealer list.
Controlled live verification is ready to run: `python -m dealerflow live-check` (integration-setup §7).

## Not verified against live services (credentials needed)

| Integration | Status | What's needed to verify |
|---|---|---|
| Gmail API send/read | Code + mocked-HTTP test only | Google Cloud project, OAuth client, refresh token (`scripts/google_oauth.py`); then Integrations → Verify, then one human-approved send to your own address |
| SMTP/IMAP | Code + fake-SMTP test only; IMAP fetch untested | Mailbox credentials (app password or relay) |
| Google Calendar free/busy + events | Code + mocked-HTTP test only | Same Google project with Calendar API + scopes |
| Zoom Server-to-Server OAuth | Code + mocked-HTTP test only | Zoom S2S app (account admin) with meeting scopes |
| Zoom webhooks | Signature/validation tested locally | Public HTTPS `DF_BASE_URL` + event subscription |
| Twilio click-to-call | Code + mocked-HTTP test only; status webhook untested | Twilio account, number or verified caller ID |
| Google Places (New) | **Disabled as a lead source** (terms allow storing place IDs only); client code kept | A live, non-stored lookup feature in a later phase |
| OpenStreetMap Overpass | Code + mocked-HTTP test only | None (public), but not called during the build |
| Website research | Mocked HTTP only; off by default | Enable and try on a few dealer sites |
| Microsoft 365 / Graph mail and calendar | **Not implemented** | Use SMTP/IMAP if your tenant allows it, or add a Graph provider |
| Bounce/delivery webhooks | **Not implemented.** Bounces are detected from bounce emails in the inbox (matched by quoted Message-ID or DSN Final-Recipient, tested with a synthetic DSN). `delivered` is never set (SMTP/Gmail don't report it) | `live-check` bounce step |

## Functional gaps

- **Reply classification is rule-based** (English/Spanish keyword patterns). It handles the common cases in
  `tests/test_classify.py` but will misread unusual phrasing. Low-confidence results are escalated to a person.
  Classifications can be corrected in the inbox.
- **Auto-answers are keyword-matched** to approved knowledge items. There's no language model drafting. The agent
  never writes free text of its own; it only sends approved text.
- **Inventory activity** is a rough lower bound: distinct "year make" strings on the first inventory page.
  Many dealer sites render inventory with JavaScript, so this will often read 0 or miss the page.
- **Dealership type** comes from TxDMV `DealerType` (franchise, or blank → independent, marked unverified) or OSM tags;
  other leads stay `unknown` until a person sets it.
- **No automatic TxDMV download.** The `.xls` is downloaded by hand and imported with the TxDMV profile (column layout
  verified against the 2026-10-18 file). If TxDMV changes the columns, the import refuses the file rather than guessing.
- **Only business-domain emails are used for outreach.** In Fort Bend that's 33 of 191 dealers; the rest need a person
  to verify a contact first. Scores on TxDMV-only data top out around 58 until websites are researched.
- **Meeting time selection is manual.** The app suggests real free slots, but a person copies them into the reply and
  records which one the prospect accepted. It does not parse times out of replies.
- **Booking approval is always manual** (manager), including for agent-handled meeting requests.
- **Daily cap is per UTC day**, not local day.
- **The suppression list cannot be edited from the UI** (by design). Removing an entry needs a direct database change.
- **The rate limiter is in-process.** Running several worker processes multiplies the per-provider HTTP limits.
  The job claim is still safe across processes. Run one worker.
- **SQLite is the default.** Under concurrent web and worker writes, SQLite serialises them (WAL mode, 30 s timeout).
  Use Postgres for anything beyond one or two users; the suite passes on Postgres 16.
- **Proposals are records only**: there's no document generation or e-signature.
- **Cost tracking** records *estimated* cost per discovery run (editable estimates; free sources default to $0), plus actual costs you enter. Provider invoices
  are not imported. CAC is shown only when won clients and costs exist.
- **No UI for editing a prospect's core fields** (address, phone, website) after creation. New sources fill missing
  fields, and conflicts are kept as evidence. Use merge or add notes; Phase 2 should add an editor with evidence capture.
- **Email open/click tracking is not implemented** (deliberately: no tracking pixels).

## Deployment

- Docker image built and the full stack run in containers (with a sandbox-only CA overlay for `pip`); migrations,
  restart recovery, backup and restore verified. See `docs/audit/deployment-recovery.md`. Not yet built on a normal host.
- `DF_BASE_URL` must be public HTTPS for unsubscribe links and webhooks. No reverse-proxy config is included.
- Backups are a command (`backup`), not a schedule; off-host, encrypted copies are the operator's job.

## Security notes

- **Secrets are not encrypted at rest (production blocker S1).** They can now come from files (`DF_SECRETS_DIR`,
  Docker/Compose secrets) instead of `.env`. The mitigation plan is in `docs/audit/security-findings.md`.
  OAuth refresh tokens are never stored in the database.
- Data at rest (database, backups) is unencrypted; use an encrypted volume and encrypted off-host backups.
- No multi-factor authentication. Passwords use PBKDF2-SHA256 (390k iterations). Failed logins are rate-limited per email+IP
  (in-memory, so it resets on restart).
- The session cookie is `Secure` only when `DF_ENV=production`; run behind HTTPS.
- API keys are per user, shown once and stored hashed. There is no expiry; regenerate to revoke.

## Legal

- **Texas Chapter 302 (telephone solicitation registration, $10,000 bond) is unresolved.** The app positions calls as
  meeting-booking only (the §302.059(1) exemption), but whether a Zoom meeting counts as a "face-to-face" presentation
  hasn't been confirmed. Get advice, or register, before repeated cold calling.
- SMS is deliberately absent (Texas SB 140).

The compliance rules in the code (CAN-SPAM footer and opt-out, suppression, Texas calling hours, no autodialing or
recording) reflect the research in `docs/research/`. They are **not legal advice**. Have counsel review
templates, the data sources you choose, and any move to automated (`auto`) sending before going live.
