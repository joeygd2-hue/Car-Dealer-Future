# Integration setup and status

_Last verified: 2026-10-18 (Phase 3.5). Research sources: `docs/research/messaging-and-outreach-access.md`._

## Status categories (honest classification)

| Category | Meaning | Providers |
|---|---|---|
| **A. Implemented and tested with real provider accounts** | called the real provider with real credentials | **None.** No credentials exist in this environment and no account was accessed. |
| **B. Implemented, tested only with mocks/sandbox** | code written against the documented API; tests use mocked HTTP or the sandbox adapter | Meta Messenger, Twilio SMS, SMTP (notifications + outreach), IMAP, Google Calendar, Zoom, Google Places, Anthropic (rephrasing) |
| **C. Implemented and fully exercised (first-party, no third party)** | no external provider involved | Website chat API + widget, ADF/XML HTTP intake, CSV prospect import, Marketplace human-assisted intake, sandbox adapters |
| **D. Partially implemented** | some pieces exist | Messenger Data Deletion callback (implemented + signature-checked; status page is a stub); per-dealership Google Calendar (OAuth refresh-token flow only, no consent UI) |
| **E. Not implemented** | — | Instagram DM, WhatsApp Cloud API, Meta Lead Ads, dealer CRM APIs (VinSolutions, DealerSocket, Elead, DriveCentric), Calendly, Microsoft Graph mail. Facebook Marketplace chats: **no official API exists** (not implementable). Google Business Profile chat: discontinued by Google. |

**Gating that applies to every provider in B:** a live adapter is used only when `LIVE_INTEGRATIONS_ENABLED=true`
**and** credentials are configured **and** an unrevoked live authorization exists for the scope (Admin → Live
authorizations). With `PILOT_MODE=true` (default) a live send must additionally target a recipient on the pilot
allowlist. Otherwise the sandbox adapter runs and results are recorded as `SIMULATED`.

## Per-provider matrix

| Provider | Purpose | Credentials / config | Approvals & costs before live | Code | Tests (all mock/sandbox) | Remaining work |
|---|---|---|---|---|---|---|
| Website chat (first-party) | customer channel | none (per-dealer `public_chat_key`) | none; live replies need scope `customer_messaging.WEBSITE_CHAT` per dealership + allowlisted visitor in pilot | `app/web/public.py`, `static/chat-widget.js` | HTTP API, rate limit, pilot allowlist, smoke run | TCPA consent text if phones are collected for SMS; websockets optional |
| ADF/XML intake | listing-site/CRM leads | per-connection bearer token (hash stored) | configure provider to POST (or an email→HTTP bridge) | `app/services/inbox/adf.py` | auth, dedupe, vehicle link, XXE/entity blocking, 1 MB limit | email (SMTP inbox) intake variant |
| Meta Messenger (Page) | customer channel | `META_APP_SECRET`, `META_VERIFY_TOKEN`; Page access token per dealer (encrypted via Admin → client → connect) | Meta app; App Review for `pages_messaging` (+ `pages_manage_metadata`, `pages_show_list`); Business Verification; HUMAN_AGENT feature for replies 24h–7d. Free. | `integrations/meta_messenger.py`, `/webhooks/meta`, `/webhooks/meta/data-deletion` | signature (valid/invalid), dedupe, tenant routing, mocked Send API (200/4xx/5xx/rate-limit/timeout), pilot live path with mocked HTTP | real App Review + test Page; Conversation Routing (handover) not used |
| Twilio SMS | customer channel | per-dealer `account_sid`, `auth_token` (encrypted), number E.164 as account id | A2P 10DLC brand (~$4.50–$46) + campaign vetting ($15) + monthly campaign fee + per-segment carrier fees **[research, unverified current]**; add ALTO/PARAR/CANCELAR/BAJA to Advanced Opt-Out; documented consent | `integrations/twilio_sms.py`, `/webhooks/twilio/sms` | signature, Spanish opt-out, 21610 handling, mocked send | registration; status callbacks for delivery receipts |
| SMTP — staff notifications | alerts, digests, password reset | `SMTP_HOST/PORT/USERNAME/PASSWORD/FROM` | scope `notifications.email`; SPF/DKIM on sending domain | `integrations/email_smtp.py` | statuses SIMULATED/FAILED/SKIPPED/UNCERTAIN via sandbox | real mailbox test with allowlisted internal address |
| SMTP — outreach | prospect email | `OUTREACH_SMTP_*`, `OUTREACH_SENDER_*`, `OUTREACH_COMPANY_NAME`, `OUTREACH_POSTAL_ADDRESS` | own Google Workspace / Microsoft 365 mailbox (bulk ESPs prohibit cold email); SPF/DKIM/DMARC; scope `outreach.email`; human-verified recipient emails; legal review | same | full sandbox lifecycle, compliance gate, races, limits | real send to allowlisted internal inbox only |
| IMAP — outreach replies | inbox monitoring | `OUTREACH_IMAP_*` | scope `outreach.inbox`; app password or IMAP OAuth | same | sandbox inbox, dedupe, classification | IMAP OAuth (XOAUTH2) if the mailbox forbids app passwords |
| Google Calendar | test drives + sales meetings | `GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID`; per-dealer refresh token | Google Cloud OAuth client (scopes calendar.events + calendar.freebusy — sensitive, not restricted); scope `calendar.google` | `integrations/google_calendar.py` | sandbox calendar: free/busy, create, cancel, failure | consent flow UI for dealers |
| Zoom (Server-to-Server OAuth) | sales meetings | `ZOOM_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET` | Zoom S2S app with meeting write scope (name per Zoom docs — re-verify); paid Zoom plan likely; scope `video.zoom` | `integrations/zoom.py` | sandbox video | — |
| Google Places API (New) | prospect discovery | `GOOGLE_PLACES_API_KEY` | billing account; Text Search with contact fields ≈ $35 / 1,000 after the free tier **[research]**; scope `outreach.discovery.google_places` | `integrations/google_places.py` | sandbox source; 30-day content purge | — |
| Anthropic Claude | optional rephrasing | `LLM_ENABLED`, `ANTHROPIC_API_KEY`, `LLM_MODEL` (default `claude-opus-5-5`), `uv sync --extra llm` | API account; data-processing review (draft + verified facts are sent; customer message text is not); scope `llm.anthropic` | `integrations/llm.py` | validator rejects added facts/dropped facts (fake LLM) | live quality evaluation |

## Setup steps (when the owner approves a live test)

Never do these without explicit approval. Each step is reversible via **Admin → Pilot console → Return to sandbox**.

1. Keep `PILOT_MODE=true`. Add the internal test recipients first (Pilot console → allowlist).
2. Put credentials in the environment (never in the repo); store per-dealer tokens through Admin → client → Connect a
   provider (encrypted with `ENCRYPTION_KEY`).
3. Set `LIVE_INTEGRATIONS_ENABLED=true`, restart web + worker.
4. Record a live authorization for exactly one scope (Admin → Live authorizations) with a statement naming the test.
5. Send one message to an allowlisted internal recipient; confirm in the Pilot console send ledger that it shows `SENT`
   with a provider id, and that a message to a non-allowlisted recipient shows `BLOCKED`.
6. Revoke the authorization (or Return to sandbox) when the test ends.
