# Integration matrix (actual status — 2026-10-18)

Legend: **Tested** = exercised by automated tests · **Sandbox-verified** = full flow verified with sandbox adapter ·
**Code only** = implemented against documented API, never called against the real provider (no credentials in this environment).
Every live path additionally requires `LIVE_INTEGRATIONS_ENABLED=true` + a recorded live authorization (see shared-stack.md).

| Integration | Purpose | Status | Verified how | Needs before live |
|---|---|---|---|---|
| Website chat (first-party) | customer inbox channel | **Working** | HTTP API tests, real uvicorn+worker smoke test, widget poll | nothing third-party; add TCPA consent text if collecting phones for SMS |
| ADF/XML lead intake (`POST /api/leads/adf/{id}`) | listing-site / CRM leads | **Working** | tests (auth, dedupe, vehicle link, XXE/entity blocking) | point provider/CRM email→HTTP bridge at endpoint; replies are staff-sent |
| Facebook Marketplace | buyer chats | **Unsupported by Meta API** → human-assisted intake (paste → draft → staff pastes back) | tests | — (never automate; no credentials/cookies/scraping) |
| Meta Messenger (Page) | customer inbox channel | **Code only** (webhook verify, X-Hub-Signature-256, parsing, Send API, 24h/7-day HUMAN_AGENT rules, data-deletion callback) | adapter tests with mocked HTTP, signed webhook tests, dedupe | Meta app, App Review (`pages_messaging` etc.), Business Verification, Page token per dealership (Admin → client → connect), `META_APP_SECRET`/`META_VERIFY_TOKEN`, HUMAN_AGENT permission for >24h replies |
| Twilio SMS | customer inbox channel | **Code only** (signature validation, send, 21610 opt-out handling) | adapter tests with mocked HTTP, signed webhook test | Twilio account + number per dealer, A2P 10DLC brand+campaign, add Spanish opt-out keywords in Twilio Advanced Opt-Out, documented consent |
| Instagram / WhatsApp / Lead Ads | channels | **Not implemented** | — | see docs/research |
| Google Business Profile chat | — | **Discontinued by Google (2024)** | — | — |
| SMTP (staff notifications) | email alerts/digests | **Code only**; sandbox records SIMULATED | notification status tests | `SMTP_*`, `notifications.email` authorization |
| SMTP (outreach) | prospect email | **Code only**; sandbox verified end-to-end | outreach E2E tests + smoke test | own Workspace/M365 mailbox (bulk ESPs forbid cold email), SPF/DKIM/DMARC, `OUTREACH_*` incl. postal address, `outreach.email` authorization, human-verified recipient emails |
| IMAP (outreach replies) | inbox monitoring | **Code only**; sandbox inbox verified | outreach E2E tests | `OUTREACH_IMAP_*`, `outreach.inbox` authorization |
| Google Places API (New) | prospect discovery | **Code only**; sandbox source verified | tests use sandbox source | API key, billing, `outreach.discovery.google_places` authorization; 30-day content purge implemented |
| CSV prospect import | prospect discovery | **Working** | tests | rows must include `source_url` |
| Google Calendar | test drives + sales meetings | **Code only** (freeBusy, events insert/delete, refresh-token OAuth) ; sandbox calendar verified | appointment + booking tests | OAuth client + refresh token (scopes calendar.events + freebusy), `calendar.google` authorization; per-dealer calendars via Admin → connect GOOGLE_CALENDAR |
| Zoom (Server-to-Server OAuth) | sales meetings | **Code only**; sandbox video verified | booking tests | Zoom S2S app (`meeting:write:meeting:admin`), `video.zoom` authorization |
| Anthropic Claude | optional rephrasing | **Code only** (grounded + validator; falls back on any violation/refusal) | validator tests with fake LLM | `LLM_ENABLED`, `ANTHROPIC_API_KEY`, `uv sync --extra llm`, `llm.anthropic` authorization; data-processing review (customer text is sent to the provider) |
| Dealer CRMs (VinSolutions, DealerSocket, Elead, DriveCentric) | lead sync | **Not implemented** (partner-gated APIs) | — | partner agreements; ADF email is the interim path |
