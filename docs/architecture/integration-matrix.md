# Integration matrix

Status key: **Mock-tested** = exercised end-to-end with a SIMULATED provider. **HTTP-tested** = request and
response handling checked against mocked HTTP that follows the provider's documented API. **Live-verified** =
called against a real account. *Nothing in Phase 1 is live-verified yet.*

Policy and legal sources for each row are in `docs/research/source-register.md` (IDs in brackets).

| Capability | Provider (setting) | Auth / minimum permissions | Used for | Status | Allowed use / constraints | Cost |
|---|---|---|---|---|---|---|
| Lead discovery | Fixture (`fixture`) | none | Offline demo and tests | Mock-tested | SIMULATED, fictional data | free |
| Lead discovery | OpenStreetMap Overpass (`osm`) | none | `shop=car` within radius | HTTP-tested | ODbL: attribution required; derived databases must stay under ODbL terms; light use of the public endpoint [S-OSM] | free |
| Lead discovery | Google Places API (New) Text Search | API key | — | **Disabled for stored leads** (client code + HTTP test kept) | Policies allow storing **place IDs only**; names, addresses and phones may not be stored [S-GPT] | 1,000 free Enterprise searches/month, then $35 per 1,000 [S-GPRICE] |
| Lead discovery | **TxDMV active dealer license list** (CSV import) | none (manual download) | Primary list of licensed Texas dealers | Mock-tested import incl. license columns | Public government list, updated daily [S-TXDMV] | free |
| Lead discovery | Dealer websites (`DF_WEBSITE_RESEARCH_ENABLED`) | none | Public homepage + one inventory page | HTTP-tested | robots.txt honoured (5xx/unreachable = disallow), identifying user agent, ≤2 pages per site, no logins or forms, no CAPTCHA bypass [S-ROBOTS] | free |
| Lead discovery | CSV / XLSX import | n/a | Lists you're entitled to use (e.g. a public license list) | Mock-tested | Must record source; operator is responsible for list rights | — |
| Send email | Mock (`mock`) | n/a | Default | Mock-tested | SIMULATED | free |
| Send + read email | SMTP + IMAP (`smtp`) | Mailbox credentials (app password / relay); IMAP read-only | Outreach, replies | HTTP-tested (fake SMTP); IMAP untested | Mailbox provider's sending limits and anti-spam rules; SPF/DKIM/DMARC [S-GBULK] | mailbox plan |
| Send + read email | Gmail API (`gmail`) | OAuth: `gmail.send`, `gmail.readonly` (restricted scope) | Outreach, replies, threading | HTTP-tested | Workspace/consumer daily sending limits; restricted-scope verification for External apps [S-GMAIL] | Workspace plan |
| Email (rejected) | SendGrid / Postmark / Mailgun etc. | — | Not used for cold outreach | — | AUPs restrict unsolicited/purchased-list email [S-ESP] | — |
| Availability | Google Calendar (`google`) | OAuth: `calendar.freebusy` | Free/busy before suggesting times | HTTP-tested | — [S-GCAL] | free with account |
| Calendar events | Google Calendar (`google`) | OAuth: `calendar.events` | Create/update/delete the booked event, invite attendee | HTTP-tested | Deterministic event IDs prevent duplicates | free with account |
| Video meetings | Zoom Server-to-Server OAuth (`zoom`) | Account credentials grant; meeting read/write scopes for one user; `user:read` for the check | Create/update/delete meetings | HTTP-tested | Rate limits by plan; JWT apps are deprecated [S-ZOOM] | Zoom plan |
| Meeting events | Zoom webhooks | Secret token (HMAC-SHA256 signature) | meeting.ended / meeting.deleted | Signature tested | Public HTTPS endpoint needed | — |
| Calls | Manual (`manual`) | none | tel: link + outcome log | Mock-tested | Human dials; calling-hours check | — |
| Calls | Twilio (`twilio`) | Account SID + auth token; owned/verified caller ID | Agent-first click-to-call bridge | HTTP-tested | No autodialer, no prerecorded/AI voice, no recording, accurate caller ID [S-TCPA][S-TX304] | per-minute |
| Notifications | Configured email provider | as above | Daily digest to `DF_NOTIFY_EMAIL` | Mock-tested | Internal email | — |

## Phase 4 integrations

| Capability | Provider (setting) | Auth / minimum permissions | Used for | Status | Allowed use / constraints | Cost |
|---|---|---|---|---|---|---|
| Payments (webhooks) | Stripe (`DF_STRIPE_WEBHOOK_SECRET`) | Endpoint signing secret; events `invoice.paid`, `invoice.payment_failed`, `charge.refunded` | Record confirmed payments and refunds against invoices | Signature, dedup and processing tested with locally signed payloads; **no Stripe account connected** | Card details are never stored; only an allow-list of event fields is kept | Stripe processing fees (per Stripe pricing) |
| Accounting | QuickBooks Online / Xero | OAuth 2.0 (sandbox companies available) | — | **Not implemented.** Manual entry + CSV export + bank-CSV reconciliation instead | — | — |
| Bank data | Bank statement CSV import | n/a | Reconciliation | Tested | Import only; no bank credentials stored | free |
| Provider billing | Billing CSV import (date, metric, quantity, cost) | n/a | Verified usage costs | Tested | Marks rows `verified_billing` | — |
| AI | Claude API (`DF_AI_ENABLED`, `DF_ANTHROPIC_API_KEY`) | API key | Optional; records measured tokens and estimated cost | Tested with a stubbed SDK client; **no live call made** | AI output never sent without approval | Opus 5.5 $4 / $20 per MTok in/out (list price, cached 2026-10-06; re-verify) |
| Website performance | Google PageSpeed Insights API v5 (`DF_PAGESPEED_API_KEY`) | API key | Mobile Lighthouse score, LCP, CLS, TBT in SEO audits | Parser written against the documented response; **not called** | Within quota | free within quota |
| SEO crawl | Client dealership websites | Written authorization recorded per site | Audits | Tested against an in-memory site | robots.txt, ≥0.5 s delay, page cap, stop on 429, GET/HEAD only, never modifies the site | free |
| Organic search | Google Search Console | — | Clicks/impressions in client reports | **API not implemented**; CSV export import tested | Authorized property owner exports the performance report | free |
| Client email | Same provider as outreach (`DF_CLIENT_EMAIL_SENDING_ENABLED`) | as above | Monthly reports, lead summaries | Mock-tested; real sending gated per client | Transactional email to the client's own contact | mailbox plan |
| Customer messaging: website chat (Phase 3) | First-party chat widget (`/static/chat-widget.js`, `POST/GET /api/chat/{key}/messages`) | Per-dealership public key; CORS limited to the dealership website | Car-buyer inquiries and replies | HTTP-tested. Replies SIMULATED unless `DF_CUSTOMER_MESSAGING_LIVE_ENABLED` + `connected` connection; pilot allowlist on by default. **Never shown to a real visitor so far** | Rate limits 30/min/IP, 8/min/visitor | free |
| Customer messaging: ADF/XML leads (Phase 3) | `POST /api/leads/adf/{connection_id}` | Bearer token (SHA-256 stored in the connection config) | Leads from listing sites/CRMs | HTTP-tested with a synthetic lead; no real lead source connected | XML entity/DTD attacks rejected (defusedxml); replies are sent by staff outside the platform | — |
| Customer messaging: Facebook Marketplace (Phase 3) | Staff-assisted (paste the buyer message) | none | Drafted replies staff copy back | Tested | No API for Marketplace messages; nothing is sent from the platform | free |
| Customer messaging: Messenger / SMS (Phase 3) | Meta Messenger, Twilio SMS | — | — | **Live adapters and webhooks not ported**; conversations on these channels use a sandbox adapter (SIMULATED) | — | — |
| Test-drive calendar (Phase 3) | Sandbox calendar (`calendar` connection) | — | Free/busy check and event on confirmation | SIMULATED only; Google Calendar adapter from Phase 1 not wired to test drives | Staff confirm every appointment | — |
| Listing publication (Phase 2) | — | — | Marketplace publishing | **Not connected.** Every publish attempt is `manual_required` | — | — |

## Explicitly not built

| Option | Reason |
|---|---|
| Automated or prerecorded/AI-voice calling, voice drops | TCPA restrictions on artificial or prerecorded voice; risk with business cell phones [S-TCPA][S-FCC-AI] |
| SMS / text outreach | Texas SB 140 (2025) brought texts into the telephone-solicitation rules; consent and registration questions unresolved [S-TXSB140] |
| Scraping Google Maps, Yelp or social sites | Their terms prohibit it; official APIs only |
| Buying email lists | ESP and mailbox-provider policies; deliverability and legal risk |
| Microsoft Graph | Not needed for a Google Workspace setup; SMTP/IMAP covers M365 where allowed. Candidate for a later phase |
| Open/click tracking | Not needed; avoids tracking pixels |

## Phase 2: inventory sources and listing channels

Research: `docs/research/meta-and-inventory-integrations-2026-10.md`, sources in `docs/research/phase2-source-register.md`.
*Nothing in Phase 2 is live-verified yet.*

| Capability | Provider | Auth / minimum permissions | Used for | Status | Allowed use / constraints | Cost |
|---|---|---|---|---|---|---|
| Inventory | CSV/TSV, XLSX, JSON upload | dealership or agency user | Dealer files, DMS exports (generic column mapping) | Mock-tested | Original rows kept; unreadable values flagged, never guessed | — |
| Inventory | Authorised HTTPS feed (`feed_api`, `website_feed`, `dms_export`) | Optional bearer/header/basic credential from `DF_FEED_*` env var | Scheduled sync, change/removal detection | HTTP-tested | Dealer authorisation note required; HTTPS + public address only (SSRF guard); no HTML scraping | — |
| Inventory | DMS vendor APIs (Dealertrack Opentrack etc.) | Partner agreements | — | Not implemented | REPORTED: no public APIs; use exports | — |
| Inventory | NHTSA vPIC VIN decode | none | — | Not implemented (offline check digit + model-year check only) | Public API, no fee listed | free |
| Publish | Facebook Marketplace (`facebook_marketplace`) | none stored; a person posts from their own account | Listing package, copy/export, record publication/edit/removal | Mock-tested (manual workflow) | **No API for organic Marketplace vehicle listings** (partner feeds ended 2021-09-13; Page listings ended 2023-01-30). No browser automation (Meta Terms). Buyer messages not API-accessible | free |
| Publish | Meta vehicle catalog / Automotive Inventory Ads (`meta_catalog`) | System-user token (`catalog_management` + `business_management`, App Review, Business Verification); `DF_META_LIVE_PUBLISHING_ALLOWED=true` + connection authorised | Catalog upsert/delete via `items_batch`, status via `check_batch_request_status` | HTTP-tested | Paid ads (can show in the Marketplace placement), not organic listings. ~8+ batch calls/min/catalog | ad spend |
| Publish | Mock channel (`mock`) | none | Tests and demos | Mock-tested | SIMULATED; never counted as an API publication in reports | free |
| Publish | CSV export of approved listings | dealership user | Manual upload to Commerce Manager or a syndication provider | Mock-tested | Export ≠ publication | — |
| Publish | Google vehicle ads, Cars.com/Autotrader/CarGurus feeds, Craigslist bulk | — | — | Not implemented | See research brief | — |
| AI | Anthropic Claude (`DF_LISTING_AI_ENABLED` + `DF_ANTHROPIC_API_KEY`) | API key | Listing prose only; facts, features, FAQ, disclaimers stay deterministic | Mocked client test | Output re-checked by guardrails; measured token cost in the audit log | per token |
