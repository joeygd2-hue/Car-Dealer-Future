# Feasibility report: Phase 1 (prospecting, outreach, scheduling)

Researched 2026-10-18. Source IDs in [brackets] refer to `source-register.md`. **This is engineering
research, not legal advice.** Items marked *Unresolved* need a lawyer or a provider's confirmation
before live use.

## 1. Bottom line

| Question | Answer |
|---|---|
| Can we build a lawful B2B prospecting + email outreach system for Texas used-car dealers? | **Yes**, with CAN-SPAM compliance (identity, postal address, ad disclosure, working opt-out honoured promptly) [S-CANSPAM]. |
| Best lead source we may store? | **The TxDMV active dealer license list** (public, daily-updated, downloadable) [S-TXDMV], plus **OpenStreetMap** with attribution [S-OSM]. |
| Can we store Google Places results as leads? | **No.** Google's Places policies allow storing place IDs only, not business details [S-GPT]. Places discovery was disabled in the app. |
| Which email route? | **Your own Google Workspace (or similar) mailbox** via Gmail API or SMTP, at low volume. **Not** SendGrid or Mailgun: their policies forbid email without prior affirmative consent [S-ESP]. |
| Calling? | **Human-dialled only.** No autodialer, no prerecorded or AI voice [S-FCC-AI]. Federal DNC rules mostly don't cover B2B calls [S-TSR][S-DNC], but **Texas Chapter 302 may require registration and a $10,000 bond** unless a call only arranges a later meeting [S-TX302]. |
| Texting? | **Not built.** Texas SB 140 brought texts under the telemarketing statutes from Sept 1, 2025 [S-TXSB140]. |
| Calendar + Zoom? | Yes: Google Calendar free/busy + events, Zoom Server-to-Server OAuth [S-ZOOM]. |
| Privacy law? | The Texas Data Privacy and Security Act excludes people acting in a commercial context, and SBA small businesses are largely exempt [S-TDPSA]. Business contact data for B2B outreach is low risk under it. |

## 2. Lead discovery: what's permitted

| Source | Permitted use | Decision |
|---|---|---|
| **TxDMV dealer license list** | Public government list of active GDN licensees, updated daily; full spreadsheet downloadable from https://texasdmv.my.salesforce-sites.com/dealers [S-TXDMV]. | **Primary source.** Download "Motor Vehicle Dealers List" by hand (the page needs a browser), then use **Import** with the source label and URL. License number, type, county and DBA columns are kept as evidence. *Unresolved:* exact column names (check the dry-run preview); filter to independent/used dealer license types. |
| **OpenStreetMap (Overpass)** | Free. Must credit "© OpenStreetMap contributors" + ODbL; derived databases only under ODbL [S-OSM]. Public server: keep regular use around 100 queries/day [S-OVERPASS]. | **Enabled.** One run/day limit in the agent; attribution shown on records with OSM evidence. Coverage of small lots is patchy. *Unresolved:* whether the CRM is a "derived database" you'd ever distribute (you won't; internal use only). |
| **Google Places API (New)** | Content may not be cached or stored except **place IDs** (indefinitely). Logo/attribution required when shown [S-GPT]. Text Search with phone/website bills at Enterprise: 1,000 free/month, then $35 per 1,000 [S-GPRICE]. | **Disabled for stored leads.** Client code retained for a possible *live, unstored* lookup in a later phase. |
| **Dealer websites** | The dealer's own public pages. Honour robots.txt (5xx/unreachable = disallow) [S-ROBOTS]. No login, no forms, no CAPTCHA bypass. | **Enabled only when turned on**, max 2 pages per site, observations stored as *unverified* evidence. |
| **Yelp** | Not researched; not used [S-YELP]. | — |
| **Scraping Google Maps, social networks, lead brokers, purchased email lists** | Against provider terms and ESP policies [S-ESP]. | **Not built.** |

## 3. Email outreach

**Law (CAN-SPAM, applies to B2B) [S-CANSPAM]**, all enforced in `services/compliance.py` and `services/outreach.py`:

| Requirement | How the app meets it |
|---|---|
| Accurate From/Reply-To/routing | Sends from your own mailbox and identity (`DF_SENDER_*`); blocked if missing. |
| Non-deceptive subject | Templates reviewed by a person; `RE:`/`FW:` on non-replies is blocked. |
| Identify as an advertisement | Footer: "This is a commercial message from {business}" (EN/ES). |
| Valid physical postal address | `DF_BUSINESS_POSTAL_ADDRESS` in every footer; sending is blocked if empty. |
| Clear opt-out; works ≥ 30 days; no fee or extra data; at most one web page | Signed unsubscribe link (no expiry), a single confirmation page, RFC 8058 one-click POST, and "reply unsubscribe/baja". Keep `DF_SECRET_KEY` stable or old links break. |
| Honour within 10 business days | Immediate: suppression list plus do-not-contact; unsent messages cancelled. |
| Penalty context | Up to $53,088 per violating email. |

**Mailbox provider rules [S-GBULK][S-GLIMITS]:** authenticate the domain (SPF, DKIM; add DMARC), keep the
spam-complaint rate under 0.1%. Workspace allows 2,000 messages/day per user (500 on trials), and the app's default
cap is 25/day. Bulk-sender rules (> 5,000/day to Gmail) won't apply at this volume, but the app already sends the
one-click unsubscribe headers.

**Provider choice:** Transactional ESPs (Twilio SendGrid, Mailgun) require prior affirmative consent and forbid
purchased, rented or scraped lists, including generic addresses like info@ [S-ESP]. **Cold B2B outreach must not
go through them.** Use your own mailbox.

**Gmail API scopes [S-GMAIL]:** `gmail.send` is *sensitive*; `gmail.readonly` is *restricted* (verification, plus a
security assessment if data is stored on servers). For your own Workspace, create an **Internal** OAuth app
(no public verification needed). Alternatively, use SMTP + IMAP with an app password.

## 4. Calling

| Topic | Finding | App behaviour |
|---|---|---|
| Automated/AI voice | The TCPA restricts artificial or prerecorded voice. AI voices count as "artificial" (FCC, Feb 2024) [S-FCC-AI] | No prerecorded, AI or automated calls. |
| Federal Do Not Call | The TSR exempts most B2B calls [S-TSR]. The DNC registry protects residential subscribers; a cell number an owner registered could be treated as residential [S-DNC] | Call the dealership's **published business line**, not personal cells. |
| Texas calling hours | 9 a.m.–9 p.m. Mon–Sat, noon–9 p.m. Sunday (§301.051) [S-TX301] | Enforced on the Call button (`DF_CALLING_HOURS_*`). Chapter 301 targets consumer goods, so this is conservative for B2B. |
| **Texas Chapter 302 (registration + $10,000 bond)** | Covers telephone solicitations to induce purchase of any "item" (including services) by any "purchaser". Exempt if the caller doesn't try to close or make the major sales presentation on the call and instead arranges a later **face-to-face** presentation (§302.059(1)); isolated transactions (§302.061); own 2+ year customers (§302.058) [S-TX302] | Calls are positioned only to **book a meeting**; the UI says so. ***Unresolved:* whether a Zoom meeting counts as "face-to-face" under §302.059. Get legal advice, or register, before a repeated cold-calling program.** |
| Texas no-call list (ch. 304) | Applies to calls selling *consumer* goods or services [S-TX304] | B2B marketing services appear outside it. Still honour any "don't call" request (phone suppression). |
| Texting (SB 140) | Texts are now "telephone calls" under ch. 302; violations are DTPA-actionable [S-TXSB140] | **No SMS feature.** |
| Recording | Texas is one-party consent [S-TXREC] | Calls are not recorded (`record="do-not-record"`). |
| Caller ID | — | Twilio uses your own number or a verified number only. |

## 5. Calendar and Zoom

- **Google Calendar**: `freeBusy` for availability (scope `calendar.freebusy`) and events insert/patch/delete (`calendar.events`),
  with `sendUpdates=all` for invitations [S-GCAL, unverified this session].
- **Zoom Server-to-Server OAuth** [S-ZOOM]: token via `account_credentials` grant, 1-hour tokens, no refresh token; an account admin
  controls scopes. Meetings are created with a waiting room and no recording. Webhooks are verified with HMAC [S-ZOOMHOOK, unverified].
- The app never shows a time that didn't come from a live free/busy call, and it marks a meeting booked only after both providers succeed.

## 6. Privacy

TDPSA's "consumer" excludes individuals acting in a commercial or employment context, and SBA small businesses are exempt except for
the sensitive-data-sale rule [S-TDPSA]. Dealership owners contacted at work are acting commercially. The app stores only business
contact details from public sources, records each source, and sells nothing. Good practice still applies: keep only what's needed, and
delete on request (suppress and redact).

## 7. Costs (as documented 2026-10-18)

| Item | Cost |
|---|---|
| TxDMV list, OpenStreetMap | Free |
| Google Workspace mailbox | Your existing plan |
| Google Calendar API, Gmail API | No API charge |
| Zoom | Your Zoom plan (S2S apps are free to create) |
| Twilio (optional) | Per-minute voice plus number rental; check Twilio pricing |
| Google Places (if ever used live) | 1,000 free Enterprise Text Searches/month, then $35 per 1,000 [S-GPRICE] |
| Texas ch. 302 registration (if required) | $10,000 security bond plus filing fee [S-TX302] |

## 8. Assumptions

- Prospects are businesses contacted at published business addresses or lines, about a business service.
- The owner operates from Texas and sends fewer than 100 emails/day.
- No consumer (car-buyer) contact happens in Phase 1.

## 9. Unresolved questions (owner or counsel)

1. **Chapter 302:** Does booking a Zoom meeting satisfy the §302.059(1) "face-to-face" exemption, or should the business register (with a $10,000 bond) before repeated cold calling?
2. **TxDMV list:** confirm column names and which license types are independent used-car dealers; confirm there are no use restrictions on the downloaded file.
3. **Zoom scope names** for create/update/delete meetings in your account (check the app's Scopes page) [S-ZOOM].
4. **Google OAuth app type:** Internal (Workspace) vs External; External plus `gmail.readonly` triggers restricted-scope verification [S-GMAIL].
5. **Texts:** if SMS is wanted later, confirm the Nov 2025 SB 140 settlement terms [S-TXSB140].
6. **Templates:** a lawyer should review the final English and Spanish copy and the approved answers.
