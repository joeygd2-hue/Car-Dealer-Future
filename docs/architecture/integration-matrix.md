# Integration matrix

Status labels: **Implemented + tested (sandbox/mock)**: the code exists and is exercised by automated tests against
simulated providers. **Verified live**: exercised against the real provider. **Manual workflow**: no API; the
system prepares the work and a person completes and records it. **Not supported**: no permitted route exists.

No integration has been **verified live** yet: no provider credentials, app reviews or OAuth verifications were
available during Phase 2. Research behind each row is in `docs/research/` and listed in
`docs/research/source-register.md` (CONFIRMED / REPORTED / UNVERIFIED labels).

Last updated: 2026-10-18 (Phase 2 chat).

## Listing publishing channels (Phase 2)

| Channel | Mode | Operations | Credentials / approvals | Confirmation | Update / delete | Status |
|---|---|---|---|---|---|---|
| Facebook Marketplace (organic vehicle listings) | Manual workflow | Prepare package (fields, EN/ES copy, ordered photo URLs, checklist), copy/export, record publication with live URL + attestation, record edit, record removal | None stored. A dealership staff member posts from their own account | `published_confirmed` with `confirmation_type=manual_attested` only after URL + attestation. Never counted as an API publication | Human edits/deletes on Facebook, then records it. Sold vehicles → `withdrawal_required` | **Manual workflow, implemented + tested.** API publishing is **not supported**: partner catalog listings ended 2021-09-13 (CONFIRMED); Page vehicle listings ended 2023-01-30 (REPORTED). Browser automation is not used (Meta Terms) |
| Meta vehicle catalog (Automotive Inventory Ads, paid; can appear in the Marketplace placement) | API | `items_batch` UPDATE (upsert) / DELETE, `item_type=VEHICLE`; status via `check_batch_request_status` | System-user token with `catalog_management` + `business_management` (App Review; Business Verification for Advanced Access); `catalog_id`; `vdp_url_template`. Live sends also require `META_LIVE_PUBLISHING_ALLOWED=true` + connection `live_publishing` | Async: `submitted` (batch handle) → `published_confirmed` when the batch finishes without item errors. Means "accepted into catalog", not an organic listing | Supported (upsert/delete); sold → delete | **Implemented + tested with mocked HTTP. Not verified live.** Rate limit ~8+ batch calls/min/catalog (CONFIRMED). No API fee found; ads cost money |
| Sandbox channel | Simulated | create / update / delete / status | None | Async submit → confirm | Supported | Test fixture only; never reaches a platform |
| CSV export of approved listings | Export | Download EN/ES CSV of approved, available, non-stale listings | Dealer user | n/a (export ≠ publication) | n/a | Implemented. For authorised manual upload (e.g. Commerce Manager, a syndication provider) |
| Google vehicle ads (Merchant Center) | – | – | Merchant Center + Google Ads + Business Profile | – | – | Not implemented. Program is live for US dealers (CONFIRMED). Candidate for later |
| Cars.com / Autotrader / CarGurus / TrueCar | – | Full-inventory feeds via providers (FTP CSV) | Dealer subscription + feed setup | – | – | Not implemented (REPORTED feed-based only) |
| Craigslist bulk posting | – | Bulk posting interface (case-by-case approval, ~$5/listing reported) | Account + approval | – | – | Not implemented |

## Inventory sources (Phase 2)

| Source | Mode | Notes | Status |
|---|---|---|---|
| Dealer CSV / TSV upload | Upload | Header aliases + optional `column_map`; raw rows preserved | Implemented + tested |
| Spreadsheet (.xlsx) upload | Upload | First sheet (or named sheet) via openpyxl; `.xls` rejected with guidance | Implemented + tested |
| DMS export (Frazer, DealerCenter, Wayne Reaves, Dealertrack …) | Upload or authorised feed URL | Treated as CSV/XLSX with a per-source `column_map`. No public DMS APIs (Frazer REPORTED none; Dealertrack via Opentrack partner agreement, REPORTED). Vendor layouts need a real sample file before a preset is added | Generic path implemented + tested; no vendor presets (UNVERIFIED layouts) |
| Authorised inventory feed / API (feed_api) | Scheduled HTTPS pull | CSV/JSON/XLSX; bearer/header/basic credential encrypted; `authorization_note` required; size + timeout limits | Implemented + tested with mocked HTTP. No real feed connected |
| Authorised website integration (website_feed) | Scheduled HTTPS pull | Only a structured feed the dealer/website provider exposes. **HTML scraping is not implemented** | Implemented (same code as feed_api) |
| HomeNet / vAuto syndication (Cox) | FTP/SFTP drop | REPORTED dealer-requested FTP exports | Not implemented (no SFTP receiver yet) |
| Manual entry | UI/API | Same normalise/validate/diff pipeline | Implemented + tested |
| NHTSA vPIC VIN decode | – | Free public API (CONFIRMED) | Not implemented; offline VIN check-digit and model-year validation only |

## Outreach agent providers (agency level)

| Provider | Kind | Operations | Auth / approval | Cost | Status |
|---|---|---|---|---|---|
| Sandbox mailbox | Email + inbox | Send, receive injected replies | None | 0 | Implemented + tested |
| Gmail API | Email + inbox | `users.messages.send` (List-Unsubscribe headers), inbox polling via `messages.list`/`get` | OAuth refresh token. `gmail.send` is Sensitive; `gmail.readonly` is Restricted (verification + annual security assessment unless the app is Internal to the agency Workspace). CONFIRMED | API free | Implemented against docs; **not verified live**. Push (`users.watch` + Pub/Sub) not implemented; polling only |
| Microsoft Graph mail | Email + inbox | – | `Mail.Send`, `Mail.Read` | – | Not implemented |
| ESPs (SendGrid, Postmark, Resend, SES) | Email | – | – | – | **Not used for cold outreach**: their policies forbid unsolicited/purchased-list email (CONFIRMED) |
| Sandbox calendar | Calendar | Free/busy, create event + fake video link | None | 0 | Implemented + tested |
| Google Calendar API | Calendar + Meet | `freeBusy`, `events.insert` with `conferenceData` (idempotent client event id), `sendUpdates=all` | OAuth (`calendar.events`, `calendar.freebusy`) | Free | Implemented against docs; not verified live |
| Zoom / Teams | Video | – | Server-to-Server OAuth / tenant policy | – | Not implemented |
| Sandbox discovery | Discovery | Simulated dealers on `.example` domains | None | 0 | Implemented + tested |
| Google Places API (New) Text Search | Discovery | `places:searchText`, `includedType=car_dealer` | API key. Requesting website/phone bills Enterprise SKU (~$35/1k, 1k free/month, CONFIRMED). Only `place_id` may be stored indefinitely; other content purged after 30 days | Estimated per call in `cost_ledger` | Implemented against docs; not verified live |
| TxDMV licensed dealer lookup | Discovery (manual) | Operator exports the official list and uploads CSV with source attribution | Public lookup (CONFIRMED); no stated terms of use | 0 | CSV import implemented + tested |
| Dealer's own public website | Enrichment | Home + contact/about pages; robots.txt honoured; observations stored with URL + excerpt | None | 0 | Implemented; tested with fixture sites (no live crawl in tests) |
| SMS / voice (Twilio etc.) | – | – | Needs documented prior express written consent; Texas ch. 302 registration or a legal opinion | – | **Disabled by policy** (only `email` channel allowed) |
| LinkedIn | – | – | User Agreement §8.2 bans automation (CONFIRMED) | – | **Not supported** |
| Anthropic Claude | AI | Listing prose; reply classification and drafts (optional) | `ANTHROPIC_API_KEY` + feature flags | Measured from token usage | Implemented; tested with a mocked client. Disabled by default |
