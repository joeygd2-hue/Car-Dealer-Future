# Source register

All sources were accessed on **2026-10-18**. Confidence key:

- **Primary**: read on the official source (agency, statute site, or provider's own docs or terms).
- **Secondary**: from a law-firm or industry summary, or a search-result summary of the official page; not read on the primary text.
- **Unverified**: from general knowledge only; still needs checking.

This register supports engineering decisions. It is not legal advice.

## Law and regulation

| ID | Claim used in the build | Source | Confidence |
|---|---|---|---|
| S-CANSPAM | CAN-SPAM covers B2B email ("no exception for business-to-business email"). Requirements: accurate header info, non-deceptive subject, clear ad disclosure, valid physical postal address, opt-out explained, opt-outs honoured within **10 business days**, opt-out mechanism working **≥30 days** after sending, no fee and no data beyond an email address to opt out, at most a reply email or a single web page. Penalty up to **$53,088 per email** (page edited Jan 2024; inflation-adjusted yearly) | https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business | Primary |
| S-TSR | FTC Telemarketing Sales Rule: most business-to-business calls are exempt, including from National Do Not Call provisions, except calls selling nondurable office or cleaning supplies. Calls to business lines that pitch employees personal purchases are not B2B | https://www.ftc.gov/business-guidance/resources/complying-telemarketing-sales-rule | Primary (first half of page read) |
| S-DNC | FCC Do-Not-Call protection applies to *residential* subscribers. Business numbers are generally not covered. Wireless numbers registered by individuals may be treated as residential (contested in courts) | https://www.govinfo.gov/link/fr/68/44147 (68 FR 44144, 2003 rule); https://www.womblebonddickinson.com/us/insights/tcpa-defense-force/102ndr9/does-national-do-not-call-registry-protection-apply-cell-phones | Secondary |
| S-FCC-AI | FCC declaratory ruling (Feb 8, 2024): AI-generated voices are "artificial" voices under the TCPA, so prerecorded or artificial voice rules and consent requirements apply | https://cooley.com/news/insight/2024/2024-02-15-fcc-ai-generated-robocalls-illegal-under-the-tcpa; https://www.akingump.com/en/insights/ai-law-and-regulation-tracker/fcc-issues-declaratory-ruling-on-ai-generated-voice-robocalls | Secondary (law-firm summaries of the ruling) |
| S-TX301 | Tex. Bus. & Com. Code §301.051: telephone solicitation only 9 a.m.–9 p.m. weekdays and Saturday, noon–9 p.m. Sunday. Chapter 301 "consumer" means someone solicited to buy a *consumer* good or service (personal/family/household use) | https://sll.texas.gov/faqs/how-late-can-telemarketers-call; https://texas.public.law/statutes/tex._bus._&_com._code_section_301.051; https://texas.public.law/statutes/tex._bus._&_com._code_section_301.001 | Primary (statute text via public.law) + State Law Library |
| S-TX302 | Tex. Bus. & Com. Code ch. 302 (Telephone Solicitation Act): "item" = property **or a service**; "purchaser" = any person solicited. Registration with the Secretary of State and **$10,000 security** (§302.107) unless exempt. Exemptions: §302.058 (own customers, 2+ years), **§302.059(1) (calls that only arrange a later face-to-face sales presentation, no sale or major presentation on the call)**, §302.061 (isolated transactions), others | https://texas.public.law/statutes/tex._bus._&_com._code_section_302.001; …_302.059; …_302.058; …_302.061; …_302.107; chapter index https://texas.public.law/statutes/tex._bus._&_com._code_title_10_subtitle_a_chapter_302 | Primary (statute text via public.law) |
| S-TXSB140 | SB 140 (89th Leg., eff. **Sept 1, 2025**) defines "telephone call" in ch. 302 to cover texts, and ties chapters 302/304/305 violations to the DTPA (private actions). A Nov 2025 settlement reportedly exempts consent-based marketing texts from ch. 302 registration (single secondary source) | https://capitol.texas.gov/tlodocs/89R/analysis/html/SB00140F.htm; https://paulhastings.com/insights/ph-privacy/marketing-texts-in-texas-sb-140-broadens-state-telemarketing-regulations; https://www.varnumlaw.com/?p=57109 | Primary (bill analysis) + Secondary (settlement) |
| S-TX304 | Tex. Bus. & Com. Code ch. 304 (Texas no-call list): "telemarketing call" means an unsolicited call to sell a **consumer** good or service. "Telephone call" includes calls/texts to mobile numbers. List published quarterly | https://texas.public.law/statutes/tex._bus._&_com._code_section_304.002 | Primary (statute text via public.law) |
| S-TDPSA | Texas Data Privacy and Security Act, Bus. & Com. Code §541.001(7): "Consumer" excludes "an individual acting in a commercial or employment context". §541.002: does not apply to SBA-defined small businesses, except §541.107 (no sale of sensitive data without consent) | https://texas.public.law/statutes/tex._bus._&_com._code_section_541.001; …_541.002 | Primary (statute text via public.law) |
| S-TXREC | Texas is a one-party-consent state for recording calls (Penal Code §16.02 defence). The app records nothing | https://www.rcfp.org/reporters-recording-guide/texas/ | Secondary |

## Data sources

| ID | Claim | Source | Confidence |
|---|---|---|---|
| S-TXDMV | TxDMV publishes a daily-updated list of active GDN dealer licensees, searchable by name, DBA, license number, city or county. A complete spreadsheet of active dealers can be downloaded. Portal: https://texasdmv.my.salesforce-sites.com/dealers ("Motor Vehicle Dealers List"). The download is behind a JavaScript page, so it's manual; column names not yet confirmed | https://content.govdelivery.com/accounts/TXDMV/bulletins/35ef629 (bulletin, June 8, 2023); portal fetched 2026-10-18 | Primary |
| S-GPT | Places API policies: must not pre-fetch, cache or store Places content except allowed exceptions. **Place IDs are exempt and may be stored indefinitely.** Google logo or attribution is required when displaying Places data | https://developers.google.com/maps/documentation/places/web-service/policies | Primary |
| S-GPRICE | Text Search (New): Pro 5,000 free/month then $32 per 1,000. Enterprise 1,000 free/month then $35 per 1,000. Requesting `nationalPhoneNumber`/`websiteUri` bills at Enterprise | https://developers.google.com/maps/billing-and-pricing/pricing; https://developers.google.com/maps/documentation/places/web-service/text-search | Primary (pricing) / Secondary (field→SKU via search summary) |
| S-OSM | OpenStreetMap: attribution "© OpenStreetMap contributors" plus ODbL notice required. Derived databases may be distributed only under ODbL | https://www.openstreetmap.org/copyright | Primary |
| S-OVERPASS | Public overpass-api.de: under ~10,000 queries and 1 GB per day is fine; for regular use, divide by 100. The server is often overloaded | https://wiki.openstreetmap.org/wiki/Overpass_API | Primary |
| S-ROBOTS | RFC 9309: robots.txt 4xx (unavailable) means crawling is allowed; 5xx or network failure (unreachable) means assume complete disallow. The app also treats 401/403 as disallow | https://www.rfc-editor.org/rfc/rfc9309 | Primary (fetched to confirm) |
| S-YELP | Yelp Fusion / Places API terms and pricing | — | **Not researched**: Yelp isn't used |

## Email, calendar, meetings, calls

| ID | Claim | Source | Confidence |
|---|---|---|---|
| S-GLIMITS | Google Workspace (paid) per user per day: 2,000 messages, 10,000 total recipients, 3,000 external recipients. Trial accounts: 500 messages/day | https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace | Primary |
| S-GBULK | Gmail sender guidelines: all senders need SPF or DKIM, valid PTR, TLS, spam rate < 0.3%. Bulk senders (> 5,000/day to Gmail) also need DMARC, alignment, and one-click unsubscribe (RFC 8058 headers) for marketing mail. Target spam rate < 0.10% | https://support.google.com/a/answer/81126 | Primary |
| S-GMAIL | `gmail.send` is a *sensitive* scope; `gmail.readonly` is a *restricted* scope. Restricted scopes need verification, plus a security assessment if data is stored or transmitted on servers | https://developers.google.com/workspace/gmail/api/auth/scopes | Primary |
| S-GCAL | Calendar `freeBusy` endpoint and `calendar.freebusy` / `calendar.events` scopes | https://developers.google.com/calendar/api/v3/reference/freebusy/query | Unverified (from API knowledge; not fetched this session) |
| S-ESP | Twilio (SendGrid) email policy forbids purchased or rented lists, and lists without affirmative consent, including scraped addresses and generic aliases like info@. It requires prior affirmative consent for non-transactional email. Mailgun's AUP forbids bought, rented or scraped lists | https://twilio.com/legal/service-country-specific-terms/email (Primary); https://mailgun.com/legal/aup (Secondary via search) | Primary / Secondary |
| S-ZOOM | Zoom Server-to-Server OAuth: `POST https://zoom.us/oauth/token` with `grant_type=account_credentials&account_id=…`, Basic auth (client_id:secret). Token lasts 1 hour; no refresh token. Account admins control available scopes | https://developers.zoom.us/docs/internal-apps/s2s-oauth/ | Primary |
| S-ZOOMHOOK | Zoom webhook signature `v0=HMAC-SHA256(secret, "v0:{ts}:{body}")` and `endpoint.url_validation` challenge | Zoom webhook docs | Unverified (from API knowledge; not fetched) |
| S-TWILIO | Twilio Calls API agent-first bridge with TwiML `<Dial>`; request signature `X-Twilio-Signature` (HMAC-SHA1) | Twilio docs | Unverified (from API knowledge; not fetched) |
| S-ONE2ONE | FCC "one-to-one consent" rule status after the 11th Circuit (Jan 2025) | — | Unverified, not needed: the app doesn't make consent-based autodialed calls |
