# Research Brief: Autonomous B2B Outreach Agent for Used-Car Dealers (Sugar Land / Houston, TX)

Date: 2026-10-18. Labels: **CONFIRMED** = I read it on an official page at the cited URL. **REPORTED** = from law-firm or other secondary sources, or search snippets. **UNVERIFIED** = my inference or background knowledge, not checked. This brief is not legal advice.

---

## 1. Prospect discovery

### Google Places API (New): Text Search
- **CONFIRMED**: the endpoint is `POST https://places.googleapis.com/v1/places:searchText`. It takes a JSON body with `textQuery` and the headers `X-Goog-Api-Key` and `X-Goog-FieldMask`. The field mask is required, there is no default set of fields, and leaving it out returns an error. `*` works but is discouraged in production. Source: https://developers.google.com/maps/documentation/places/web-service/text-search
- **CONFIRMED** (same page): **billing depends on the highest-tier field in the mask.**
  - Essentials ID Only: `places.id`, `places.name`, `places.attributions`, `nextPageToken`
  - Pro: `places.displayName`, `places.formattedAddress`, `places.types`, `places.location`
  - Enterprise: `places.websiteUri`, `places.nationalPhoneNumber`, `places.rating`
- **CONFIRMED** (same page):
  - `includedType` (for example `car_dealer`) biases results to one Table A type. `strictTypeFiltering: true` makes the filter strict.
  - `pageSize` is 1–20 and paging uses `pageToken`.
  - **Results are capped at 60 across all pages.** You need many narrow queries (by ZIP or neighborhood: Sugar Land, Stafford, Missouri City, Alief, etc.).
- **CONFIRMED** pricing per 1,000 calls in the 0–100K tier, from https://developers.google.com/maps/billing-and-pricing/pricing:

  | SKU | Price | Free calls per month |
  |---|---|---|
  | Text Search Essentials (IDs Only) | none listed | unlimited |
  | Text Search Pro | $32.00 | 5,000 |
  | Text Search Enterprise | $35.00 | 1,000 |
  | Enterprise + Atmosphere | $40.00 | 1,000 |

  A mask that includes `websiteUri` or `nationalPhoneNumber` is billed as **Enterprise**.
- **CONFIRMED** storage rules, from https://developers.google.com/maps/documentation/places/web-service/policies:
  - "You must not pre-fetch, cache, or store Places API content beyond the allowed exceptions."
  - Place IDs are exempt: "You can therefore store place ID values indefinitely."
- **CONFIRMED** Service Specific Terms §14, from https://cloud.google.com/maps-platform/terms/maps-service-terms:
  - Latitude and longitude may be cached for up to 30 consecutive calendar days, then deleted.
  - Places content must not be used with a non-Google map.
- **UNVERIFIED** (my reading of the terms): keeping Google-returned name, address, phone and website permanently in a CRM is a caching risk. Two safer designs:
  - Store only `place_id`, and re-fetch details when needed.
  - Use Places only to find the dealer's own website. Then take contact data, including email, from the dealer's own site or from TxDMV records.
- **UNVERIFIED**: Places does not return email addresses, so emails must come from the dealer's own published contact info.

### Texas DMV (TxDMV) GDN dealer data
- **CONFIRMED** (official TxDMV GovDelivery bulletin, 2023-06-13, https://content.govdelivery.com/accounts/TXDMV/bulletins/35fdc87):
  - TxDMV runs a public license lookup at https://texasdmv.my.salesforce-sites.com/dealers.
  - It can be searched by business name, DBA, license number, city or county. It is "updated daily".
  - "a complete spreadsheet of active dealers is also available for download".
  - Status "APA Expired" should be treated as active.
- **CONFIRMED**: I loaded the lookup page. It links to "Motor Vehicle Dealers", "Salvage Dealers", "Closed License Search" and other lists. These run through JavaScript and JSF postbacks, so there is no stable direct download URL to cite.
- **CONFIRMED**: the bulletin states no terms of use.
- **UNVERIFIED**: no dealer-license dataset was found on data.texas.gov. Search results did not surface one.
- **Practical use (UNVERIFIED)**: filter the TxDMV list to Fort Bend and Harris counties. Use it to confirm that a prospect is a licensed independent (GDN) dealer, and match it to Places `place_id`. Store TxDMV fields (public government data) as the record of truth instead of Google content.

---

## 2. Email sending: APIs and acceptable-use policies

### Gmail API
- **CONFIRMED** scope classes, from https://developers.google.com/workspace/gmail/api/auth/scopes:
  - `gmail.send` is **Sensitive** and needs OAuth app verification.
  - `gmail.readonly`, `gmail.modify` and `gmail.compose` are **Restricted**. They need restricted-scope verification, plus a **security assessment** if the app stores or transmits the data on servers.
- **CONFIRMED** (https://support.google.com/cloud/answer/13465431): the security assessment is the last step of restricted-scope review and leads to a Letter of Validation. "All applications must be revalidated every year."
- **UNVERIFIED**: an "Internal" OAuth app (Workspace users in the agency's own domain only) does not need public verification. This may avoid the assessment for the agency's own inboxes. Check the Google Cloud consent-screen docs.

### Microsoft Graph `sendMail`
- **CONFIRMED** (https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0):
  - Endpoints are `POST /me/sendMail` and `POST /users/{id}/sendMail`.
  - The permission is `Mail.Send` (delegated or application).
  - It returns `202 Accepted`. Delivery is "subject to Exchange Online limitations and throttling".
  - Custom `internetMessageHeaders` are supported.
- **UNVERIFIED**: whether those custom headers allow `List-Unsubscribe`, and the exact Exchange Online recipient-rate limits. Not checked.

### ESP acceptable-use policies on cold email
| ESP | Policy | Source |
|---|---|---|
| **Resend** | **CONFIRMED** forbidden: "unsolicited messages of any kind, including cold outreach, purchased lists". "All mail must be sent to recipients who have explicitly opted in." | https://resend.com/legal/acceptable-use |
| **Twilio SendGrid** | **CONFIRMED** forbidden: "you must obtain affirmative consent prior to sending any emails". Bans "purchased or rented email lists". Scraped addresses and generic aliases (info@) also need prior consent. | https://twilio.com/legal/service-country-specific-terms/email |
| **Postmark** | **CONFIRMED** forbidden: "All email lists ... must be permission-based subscriptions". "Use of a list that has been purchased or rented from a third party is prohibited." | https://postmarkapp.com/terms-of-service |
| **Amazon SES** | **CONFIRMED**: the AWS AUP bans sending "unsolicited mass email or other messages, promotions, advertising, or solicitations." | https://aws.amazon.com/aup/ |

**Takeaway (UNVERIFIED inference):**
- None of the four ESPs is suitable for cold outreach. All of them prohibit it or require opt-in.
- Low-volume, one-to-one sales email from the agency's own Google Workspace or Microsoft 365 mailbox is the realistic channel.
- It must still follow CAN-SPAM, the providers' anti-spam terms and the sender requirements below.
- Use ESPs only for transactional mail, such as meeting confirmations to people who have engaged.

### Gmail and Yahoo sender requirements
- **CONFIRMED** Gmail (https://support.google.com/a/answer/81126):
  - **All senders:** SPF or DKIM, valid forward and reverse DNS (PTR), TLS, RFC 5322 format, spam rate below 0.3% in Postmaster Tools.
  - **Bulk senders (more than 5,000 per day):** SPF, DKIM and DMARC (`p=none` is enough), From-domain alignment, and one-click unsubscribe headers `List-Unsubscribe-Post: List-Unsubscribe=One-Click` plus `List-Unsubscribe: <https://...>` (RFC 8058), along with a visible unsubscribe link in the body.
  - The recommended spam rate is below 0.10%, and senders should never reach 0.30%.
  - DKIM keys must be at least 1024 bits; 2048 is recommended.
- **CONFIRMED** Yahoo (https://senders.yahooinc.com/best-practices/): SPF and DKIM, DMARC of at least `p=none` that passes with alignment, one-click List-Unsubscribe (RFC 8058 recommended), a visible unsubscribe link, unsubscribes processed **within 2 days**, and a complaint rate below 0.3%.
- **UNVERIFIED** recommendation: implement the bulk-sender rules (DMARC, RFC 8058, 2-day suppression) even below 5,000 per day. Use a separate sending subdomain to protect the main domain.

---

## 3. Inbox monitoring

- **CONFIRMED** Gmail push (https://developers.google.com/workspace/gmail/api/guides/push):
  1. Create a Pub/Sub topic and give `gmail-api-push@system.gserviceaccount.com` publish rights on it.
  2. Call `POST /gmail/v1/users/me/watch` with `topicName`. `labelIds` such as `INBOX` are optional.
  3. Each notification carries base64url JSON with `emailAddress` and `historyId`.
  4. Then call `history.list` from the last stored `historyId` to get the changes.
  - **Renew the watch at least every 7 days** (Google recommends daily).
  - Notifications are limited to 1 per second per user, and they can be delayed or dropped, so fall back to periodic `history.list`.
- **UNVERIFIED**: needed scopes are `gmail.readonly`, or `gmail.modify` to label or archive. Both are Restricted (see section 2).
- **CONFIRMED** Microsoft Graph (https://learn.microsoft.com/en-us/graph/outlook-change-notifications-overview and https://learn.microsoft.com/en-us/graph/change-notifications-overview):
  - Create a subscription with `POST /subscriptions` on `/users/{id}/messages` or `/me/mailFolders('inbox')/messages`.
  - Permission is `Mail.ReadBasic` or `Mail.Read`, delegated or application.
  - Maximum lifetime is **10,080 minutes (under 7 days)**, or 1,440 minutes when resource data is included.
  - There is a limit of 1,000 subscriptions per mailbox. Lifecycle notifications are recommended.

---

## 4. Meeting booking

- **CONFIRMED** Google Calendar `events.insert` (https://developers.google.com/workspace/calendar/api/v3/reference/events/insert):
  - Endpoint is `POST https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events`.
  - `conferenceDataVersion=1` is required to create conferences through `conferenceData.createRequest`.
  - Accepted scopes: `calendar`, `calendar.events`, `calendar.app.created`, `calendar.events.owned`.
  - `sendUpdates=all` emails the invitees.
- **CONFIRMED** Google Meet links (https://developers.google.com/workspace/calendar/api/guides/create-events):
  - Send a `createRequest` with a unique `requestId`. The solution type is `hangoutsMeet`.
  - Creation is asynchronous: the status goes from `pending` to `success`.
  - Check `conferenceProperties.allowedConferenceSolutionTypes` to see what the calendar allows.
- **CONFIRMED** `freebusy.query` (https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query):
  - Endpoint is `POST https://www.googleapis.com/calendar/v3/freeBusy`.
  - Accepted scopes: `calendar.freebusy`, `calendar.events.freebusy`, `calendar.readonly` or `calendar`.
- **UNVERIFIED**: Calendar scopes are "sensitive", not restricted, so no security assessment is needed.
- **CONFIRMED** Zoom Server-to-Server OAuth (https://developers.zoom.us/docs/internal-apps/s2s-oauth/):
  - `POST https://zoom.us/oauth/token` with `grant_type=account_credentials&account_id=...` and Basic client credentials.
  - Tokens last 1 hour and there is no refresh token.
  - Scopes are chosen in the app settings.
- **REPORTED**: the create-meeting call is `POST https://api.zoom.us/v2/users/{userId}/meetings` with scope `meeting:write:meeting` (or the `:admin` variant). Source is the Zoom ISV workflow page seen in search, https://developers.zoom.us/docs/isv/workflows/create-meeting/, which I did not open. For S2S apps, use an explicit host user ID rather than `me`.
- **CONFIRMED** Microsoft Teams (https://learn.microsoft.com/en-us/graph/api/application-post-onlinemeetings?view=graph-rest-1.0):
  - Endpoints are `POST /me/onlineMeetings` and `/users/{userId}/onlineMeetings`.
  - Permissions: delegated `OnlineMeetings.ReadWrite`, or application `OnlineMeetings.ReadWrite.All`.
  - The application permission needs an **application access policy** set up by the tenant admin.
- **UNVERIFIED**: creating a Graph calendar event with `isOnlineMeeting: true` is the simpler way to get a Teams invite.

---

## 5. Compliance

### CAN-SPAM (email)
**CONFIRMED** (FTC, https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business):
- **No B2B exception.** The law covers any commercial email.
- Header information (From, To, Reply-To, routing) must be truthful.
- Subject lines must not be deceptive.
- The message must clearly disclose that it is an advertisement.
- It must include a valid physical postal address (a street address, USPS PO box or registered private mailbox).
- It needs a clear opt-out, and an option to stop all marketing.
- Opt-outs must be honored **within 10 business days**, and the opt-out mechanism must keep working **at least 30 days** after sending.
- The advertiser stays liable even when a vendor sends the mail.
- Penalties are **up to $53,088 per email**.
- Harvesting addresses is an aggravated violation.

**What the system must enforce (derived):**
- A global suppression list that is checked before every send and never expires.
- Opt-out processing within 2 days, to meet Yahoo's rule, which is stricter than CAN-SPAM's.
- A footer on every message with the sender's identity, postal address and unsubscribe link.
- An "advertisement" disclosure.
- No misleading "Re:" or "Fwd:" subject lines.
- Truthful From and Reply-To headers.
- No use of addresses harvested by automated scraping. Harvesting is an aggravated CAN-SPAM violation, and SendGrid's policy also forbids scraped addresses.
- Logs kept of consent, source, sends and opt-outs.

### TCPA (calls and texts)
- **CONFIRMED** 47 CFR 64.1200, current eCFR text (https://www.ecfr.gov/current/title-47/chapter-I/subchapter-B/part-64/subpart-L/section-64.1200, read through the eCFR API on 2026-10-01):
  - §64.1200(a)(1)(iii) requires **prior express consent** for autodialed or artificial/prerecorded-voice calls to cell numbers. Texts are treated as calls (UNVERIFIED wording, but long-settled FCC position).
  - §64.1200(a)(2) requires **prior express written consent** when the call is telemarketing or advertising. Written consent must be signed and include specific disclosures.
  - The rule makes no B2B exception for cell numbers.
  - §64.1200(a)(10): consent may be revoked by any reasonable means. "STOP", "quit", "end", "revoke", "opt out", "cancel" and "unsubscribe" are per se valid, and so are other replies a reasonable person would read as revocation. Revocations must be honored **within 10 business days**, and the caller may not designate an exclusive opt-out method.
  - The current CFR text contains **no "one-to-one" consent language**.
- **REPORTED** (McGuireWoods, Adams and Reese and others, via search): the 11th Circuit vacated the FCC's one-to-one consent rule in *Insurance Marketing Coalition v. FCC* on **Jan. 24, 2025**, the same day the FCC postponed it.
- **REPORTED** (Burr & Forman, CompliancePoint, Consumer Financial Services Law Monitor): the "revoke-all" part of the April 11, 2025 revocation rules has been delayed. It moved to April 11, 2026, then by an order of Jan. 6, 2026 to **Jan. 31, 2027**. The other revocation rules are in effect.

### Texas
- **CONFIRMED** Tex. Bus. & Com. Code ch. 304 (Texas no-call list), from https://tcss.legis.texas.gov/resources/BC/htm/BC.304.htm:
  - §304.004(3) says the chapter does not apply to a call "between a telemarketer and a business, other than by a facsimile solicitation, unless the business has informed the telemarketer that the business does not wish to receive a telemarketing call".
  - **Design implication:** the system must record a business's "do not call us" request and honor it.
- **CONFIRMED** ch. 302 (telephone solicitation registration), from https://tcss.legis.texas.gov/resources/BC/htm/BC.302.htm:
  - §302.101 bars a seller from making a telephone solicitation to a purchaser in Texas without a **registration certificate** from the Secretary of State.
  - **There is no general B2B exemption.** §302.056 exempts only sales to businesses that intend to resell, or to use the item in recycling, reuse, remanufacturing or manufacturing. Marketing services fall outside both.
  - Possibly relevant exemptions:
    - §302.058: current or former customers, if the seller has used the same business name for 2 or more years.
    - §302.059(1): no sale is completed during the call, and a major sales presentation is arranged "face-to-face at a later meeting".
  - **UNVERIFIED**: whether a video meeting counts as "face-to-face". Ask counsel.
  - §302.051 puts the burden of proving an exemption on the seller.
- **REPORTED** (Paul Hastings, Nelson Mullins, Greenberg Traurig; bill analyses at capitol.texas.gov): **SB 140 took effect Sept. 1, 2025.**
  - It brings ch. 304's definition of "telephone call" into ch. 302, so texts, image messages and autodialed calls become telephone solicitations that need registration.
  - It makes violations actionable under the DTPA, which gives a private right of action.
  - Failure to register carries a penalty of up to $5,000 per violation.

**Bottom line for SMS and voice:** disable both by default. Enable them for a contact only when there is documented prior express written consent, such as a signed web form recorded with timestamp, IP address, disclosure text and phone number. Before any outbound call or text campaign touching Texas, also do one of the following: obtain a Texas ch. 302 registration, or get a written legal opinion that an exemption applies. Dealer numbers on Google listings are often owners' cell phones (UNVERIFIED), which raises TCPA exposure. Inbound replies by phone or text are fine, and so are manual one-off calls placed by a human. TCPA consent rules target autodialed and prerecorded calls, but ch. 302 can still reach any telephone solicitation (UNVERIFIED application).

---

## 6. LinkedIn

- **CONFIRMED** (User Agreement §8.2, https://www.linkedin.com/legal/user-agreement): users may not "Use bots or other unauthorized automated methods to access the Services". That covers automated message sending and inauthentic engagement. Users also may not build or use software, scripts or robots to scrape the Services, or bypass usage limits.
- **Practical rule:** no automated connection requests, messaging or scraping. LinkedIn may only be used manually, by a human.

---

## Summary table

| Provider / integration | Operations | Auth / scopes | Approval / verification | Cost | Notes / status |
|---|---|---|---|---|---|
| Google Places API (New) | `places:searchText` (`includedType=car_dealer`) | API key + `X-Goog-FieldMask` | Billing account | Pro $32/1K (5K free per month); Enterprise $35/1K (1K free per month) for website or phone | Store only `place_id` indefinitely. Lat/lng cache 30 days max. 60 results per query. **CONFIRMED** |
| TxDMV dealer lookup | Search, or download the active-dealer spreadsheet | None (public) | None | Free | Updated daily. No stated terms. No stable download URL. **CONFIRMED** (data.texas.gov absence UNVERIFIED) |
| Gmail API | `users.messages.send`, `watch`, `history.list` | `gmail.send` (Sensitive); `gmail.readonly` / `gmail.modify` (Restricted) | OAuth verification. Restricted scopes need an annual security assessment | API free; assessment cost UNVERIFIED | Internal-app exemption UNVERIFIED. **CONFIRMED** scopes |
| Microsoft Graph mail | `sendMail`; `/subscriptions` on messages | `Mail.Send`; `Mail.ReadBasic` / `Mail.Read` | Entra app consent (admin consent for application permissions) | Included with M365 | Subscriptions last under 7 days (1 day with resource data). **CONFIRMED** |
| Resend / SendGrid / Postmark / SES | Bulk or transactional sending | API keys | Account review | Varies (not researched) | All four ban cold or unsolicited email or purchased lists. Use for transactional mail only. **CONFIRMED** |
| Google Calendar | `events.insert` + Meet (`conferenceDataVersion=1`); `freebusy.query` | `calendar.events`; `calendar.freebusy` | OAuth verification (Sensitive, UNVERIFIED) | Free | **CONFIRMED** |
| Zoom | `POST /v2/users/{id}/meetings` | S2S OAuth; `meeting:write:meeting` | Internal S2S app; admin authorizes | Zoom plan | Token lasts 1 h, no refresh. S2S CONFIRMED; scope **REPORTED** |
| MS Teams (Graph) | `POST /users/{id}/onlineMeetings` | `OnlineMeetings.ReadWrite(.All)` | Application access policy for app permissions | M365 | **CONFIRMED** |
| CAN-SPAM | Every commercial email | n/a | n/a | Up to $53,088 per violation | B2B covered. 10-business-day opt-out. **CONFIRMED** |
| Gmail / Yahoo sender rules | Authentication, unsubscribe, spam rate | SPF, DKIM, DMARC; RFC 8058 | n/a | n/a | Spam rate under 0.3% (target under 0.1%). Yahoo: unsubscribes within 2 days. **CONFIRMED** |
| TCPA / FCC | Calls and texts | Prior express (written) consent | n/a | Statutory damages (not researched) | One-to-one rule vacated (**REPORTED**). Revoke-all delayed to 1/31/2027 (**REPORTED**). Revocation within 10 business days (**CONFIRMED**) |
| Texas ch. 302 / 304 / SB 140 | Telephone solicitation, including texts | SOS registration certificate | Registration unless an exemption applies | Fee UNVERIFIED | Ch. 304 exempts B2B calls unless opted out (**CONFIRMED**). Ch. 302 has no general B2B exemption (**CONFIRMED**). SB 140 covers texts, effective 9/1/2025 (**REPORTED**) |
| LinkedIn | Messaging, connection requests | n/a | n/a | n/a | Automation prohibited (§8.2). **CONFIRMED** |
