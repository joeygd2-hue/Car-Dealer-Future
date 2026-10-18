# Authorized messaging & outreach access — research findings

Researched 2026-10-18 for Phase 3 (customer response) and the outreach agent. **Not legal advice.**
Legend: **[V]** verified on official page · **[S]** secondary source only · **[!]** conflicting / possibly stale — re-verify before relying on it.

## Part A — Customer-response channels

### 1. Facebook Marketplace — NO API ACCESS
- No public Graph/Messenger/Commerce/partner API exposes Marketplace buyer↔seller conversations. Messenger Platform docs cover Page messaging only. **[V]** https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview
- Personal-profile listing chats belong to the profile and are not reachable by Page messaging tools. **[S]**
- Since **2023-01-30** US dealers cannot list vehicles from business Pages on Marketplace. **[S]** https://jalopnik.com/facebook-will-no-longer-let-dealers-list-cars-suggests-1849603029
- Vehicle-listings partner feeds for free US listings ended **2021-09-13**; catalogs now reach Marketplace only via paid Automotive Inventory Ads. **[S]**
- Meta Terms §3.2 prohibit automated data collection without permission and collecting usernames/passwords or misappropriating access tokens. **[V]** https://www.facebook.com/terms.php

**Decision:** Marketplace is supported only as a **human-assisted workflow**: staff paste the buyer's message into the portal, the system drafts an EN/ES reply, and staff paste it back into Marketplace manually. No credentials, cookies, extensions, scraping or auto-posting.

### 2. Messenger Platform (Page messaging) — SUPPORTED (requires Meta approval)
- Permissions: `pages_show_list`, `pages_manage_metadata`, `pages_messaging`, `pages_read_engagement`, `business_management`; Advanced Access via App Review (unless only own Page); Business Verification. Free. **[V]**
- Standard 24h messaging window; `HUMAN_AGENT` tag allows human replies within 7 days (separate App Review). **[V]** https://developers.facebook.com/documentation/business-messaging/messenger-platform/policy
- Webhooks: verify-token handshake; `X-Hub-Signature-256` HMAC-SHA256 with App Secret; Meta retries and may duplicate; events may arrive out of order. **[V]** https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks
- Human takeover: Handover Protocol deprecated → Conversation Routing. https://developers.facebook.com/docs/messenger-platform/conversation-routing
- Rate limits: 200 × engaged users / 24h; Send API 300 calls/s per Page. **[V]**
- Retention: delete data promptly on request and when no longer needed (Platform Terms §3.d); Data Deletion callback required. **[V]** https://developers.facebook.com/terms/dfc_platform_terms/
- Whether Automotive-Inventory-Ad Messenger leads reach the Page webhook is an inference. **[!]**

### 3. Instagram Messaging — feasible later
`instagram_business_manage_messages`; same 24h / human-agent rules. **[V]**

### 4. WhatsApp Cloud API — deferred
Per-message (template) pricing since 2025-07-01 **[V]**; possible Oct-2026 service-message charge **[!]**. Opt-in naming the business required; templates outside 24h window. **[V]**

### 5. Meta Lead Ads — feasible later
`leadgen` webhook + `leads_retrieval` and related permissions. **[V]**

### 6. SMS via Twilio — SUPPORTED (requires A2P 10DLC registration + consent)
- 10DLC brand ($4.50–$46), campaign vetting $15, monthly $1.50–$10, carrier per-segment fees. **[S][!]**
- Twilio default opt-out keywords are English only (STOP, UNSUBSCRIBE, END, QUIT, STOPALL, REVOKE, OPTOUT, CANCEL); Spanish keywords (ALTO, PARAR, …) must be added via Advanced Opt-Out. **[V]** https://www.twilio.com/docs/messaging/tutorials/advanced-opt-out
- FCC (eff. 2025-04-11): consent revocable by any reasonable means; honor within 10 business days; max one confirmation. **[S]**
- `X-Twilio-Signature` = base64(HMAC-SHA1(auth token, URL + sorted POST params)). **[V]** https://www.twilio.com/docs/usage/webhooks/webhooks-security

### 7. Website chat & forms — SUPPORTED (first-party, no third-party approval)
Capture TCPA prior express written consent (unchecked box, not a condition of purchase) before any marketing text. Texas SB 140 (eff. 2025-09-01) extends Chapter 302 telemarketing rules to marketing texts. **[S]**

### 8. Dealer CRMs & listing-site leads — ADF/XML SUPPORTED
ADF/XML 1.0 is the de-facto lead format; Cars.com/CarGurus/Autotrader deliver ADF email. CRM APIs (VinSolutions, DealerSocket, Elead/Fortellis, DriveCentric) are partner-gated. **[S]** Implemented: ADF/XML inbound parser/endpoint.

### 9. Google Business Profile chat — DISCONTINUED (July 2024). **[V]**

### 10. Gmail API — avoid restricted scopes
`gmail.readonly`/`gmail.modify` are restricted → verification + annual CASA. **[V]** Use IMAP/SMTP with the account owner's credentials (or Internal Workspace app) instead.

| Channel | Official access? | Status in this repo |
|---|---|---|
| FB Marketplace chats | **No** | Human-assisted intake only |
| Messenger (Page) | Yes (App Review, Business Verification) | Adapter + webhook implemented; needs credentials/approval |
| Instagram DM | Yes | Not implemented |
| WhatsApp Cloud | Yes | Not implemented |
| Lead Ads | Yes | Not implemented |
| Twilio SMS | Yes (10DLC) | Adapter + webhook implemented; needs credentials/registration |
| Website chat | Yes (first-party) | Implemented & tested |
| ADF/XML leads | Yes (email/HTTP) | Implemented & tested (HTTP intake) |
| GBP chat | Discontinued | — |

## Part B — B2B outreach to Texas dealerships

### (a) Prospect data sources
- **Google Places API (New) Text Search**: Pro 5,000 free/month then $32/1k. **[V]** Only `place_id` may be stored indefinitely; other Places content must not be cached beyond permitted exceptions (some fields 30 days **[!]**); attribution required. **[V]** https://developers.google.com/maps/documentation/places/web-service/policies
  → Implementation stores `place_id` permanently and marks Places-derived fields with a 30-day expiry; a purge job clears them unless independently verified by a human.
- **TxDMV Dealer License Lookup** (searchable; bulk spreadsheet unconfirmed **[S]**) https://www.txdmv.gov/dealers/licensing → use manually / via CSV import with source attribution.
- **CSV import** of operator-researched prospects (with source URL per row) — always permitted.
- Dealer websites: no scraping implemented.

### (b) Cold email compliance
- **CAN-SPAM** applies to B2B: accurate headers, non-deceptive subject, identify as ad, physical postal address, working opt-out honored ≤10 business days. **[V]** https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
- **Gmail bulk-sender rules**: SPF/DKIM; ≥5k/day needs DMARC + RFC 8058 one-click unsubscribe; spam rate <0.3%. **[V]** https://support.google.com/a/answer/81126
- **Texas Bus. & Com. Code ch. 321**: unsolicited commercial email subject must begin with "ADV:", working reply address, honor removal within 3 days. **[V]** https://statutes.capitol.texas.gov/Docs/BC/htm/BC.321.HTM (preemption contested **[S]**) → implemented as default-on compliance rule; suppression is immediate.

### (c) Cold SMS / calls
TCPA requires prior express (written, for marketing) consent for autodialed texts/calls to wireless numbers — no B2B exemption; Texas SB 140 adds state exposure. → **Cold SMS and calls are blocked in the outreach agent.**

### (d) Sending providers
Postmark, SendGrid, Resend prohibit cold/unsolicited email. **[V]** → Outreach sends via the operator's own mailbox (SMTP) and reads replies via IMAP.

### (e) Meeting booking
Google Calendar `freeBusy.query` + `events.insert` (`conferenceDataVersion=1` for Meet). Zoom Server-to-Server OAuth (`meeting:write:meeting:admin` **[S]**). Calendly scheduling links (webhooks plan-dependent **[!]**).

## Items to re-verify before go-live
WhatsApp Oct-2026 pricing; current 10DLC fees; CTIA Spanish keyword guidance; AIA Messenger lead webhook delivery; TxDMV bulk data terms; Zoom/Graph scope names; Places 30-day field list.
