# Source register: Phase 2 (inventory and listing channels) and outreach compliance cross-check

Sources used for integration and compliance decisions. Full briefs with context:
`meta-and-inventory-integrations-2026-10.md` and `outreach-providers-and-compliance-2026-10.md` (both 2026-10-18).
Labels: CONFIRMED = official document fetched or seen in official search text; REPORTED = credible third party;
UNVERIFIED = assumption, not checked. Re-verify before relying on any item for a production decision.

| # | Topic | Claim used | Source | Label |
|---|---|---|---|---|
| 1 | Marketplace | Partner-catalog vehicle listings discontinued on Marketplace from 2021-09-13 (US + 8 countries); AIA still shown on Marketplace | https://en-gb.facebook.com/business/help/442276120199473 (title/search text; direct fetch 404) | CONFIRMED |
| 2 | Marketplace | Business-Page vehicle listings ended 2023-01-30 | https://www.jalopnik.com/facebook-will-no-longer-let-dealers-list-cars-suggests-1849603029/ | REPORTED |
| 3 | Marketplace | DealerCenter told dealers bulk upload to Marketplace ended | https://dcnation.dealercenter.com/blog/important-news-about-facebook-marketplace | REPORTED |
| 4 | Meta terms | No automated access/collection without permission | https://www.facebook.com/terms.php ; https://www.facebook.com/legal/automated_data_collection_terms | CONFIRMED |
| 5 | Meta commerce | Marketplace intended for individual sellers | https://www.facebook.com/policies_center/commerce | CONFIRMED |
| 6 | Meta catalog | Vehicle fields (required/optional, limits) | https://developers.facebook.com/documentation/ads-commerce/marketing-api/auto-ads/reference | CONFIRMED |
| 7 | Meta catalog | `items_batch` with `item_type=VEHICLE`, CREATE/UPDATE/DELETE, handles + `check_batch_request_status` | https://developers.facebook.com/docs/marketing-api/reference/product-catalog/items_batch/ ; https://developers.facebook.com/docs/marketing-api/catalog-batch | CONFIRMED |
| 8 | Meta catalog | Rate limits (catalog batch / management) | https://developers.facebook.com/docs/graph-api/overview/rate-limiting | CONFIRMED |
| 9 | Meta permissions | `catalog_management` (+`business_management`), App Review, Business Verification for Advanced Access | https://developers.facebook.com/docs/permissions/reference/catalog_management | CONFIRMED |
| 10 | Meta messaging | `pages_messaging`, 24h window, HUMAN_AGENT 7 days; Marketplace chats not available via API | https://developers.facebook.com/docs/permissions/reference/pages_messaging ; https://developers.facebook.com/docs/messenger-platform/policy/policy-overview ; Marketplace exclusion: community reports | CONFIRMED / REPORTED |
| 11 | Auto catalog | Feed formats; third-party feed providers; manual entry under 50 vehicles | https://developers.facebook.com/docs/marketing-api/dynamic-ads-auto/auto-catalog ; https://www.facebook.com/business/help/143781049600895 | CONFIRMED |
| 12 | VIN | NHTSA vPIC decode API, batch ≤50, no fee listed | https://vpic.nhtsa.dot.gov/api/ | CONFIRMED |
| 13 | DMS | Dealertrack API via Opentrack partner program; Frazer no public API; HomeNet/vAuto FTP exports | see Meta/inventory brief §5 | REPORTED |
| 14 | Google | Vehicle ads available to US dealers | see Meta/inventory brief §5 | CONFIRMED |
| 15 | Places | Text Search (New) endpoint, field masks, Enterprise SKU pricing, storage limits (place_id only indefinitely) | see outreach brief §1 (developers.google.com/maps) | CONFIRMED |
| 16 | TxDMV | Official licensed-dealer lookup https://texasdmv.my.salesforce-sites.com/dealers; spreadsheet of active dealers | TxDMV bulletin 2023-06-13 (outreach brief §1) | CONFIRMED |
| 17 | ESP policies | SendGrid, Postmark, Resend, AWS SES policies forbid unsolicited/cold email or purchased lists | see outreach brief §2 | CONFIRMED |
| 18 | Gmail | `gmail.send` Sensitive; `gmail.readonly`/`modify` Restricted (verification + security assessment) | see outreach brief §2 | CONFIRMED |
| 19 | Bulk senders | Gmail/Yahoo SPF, DKIM, DMARC, one-click unsubscribe, spam rate <0.3% | see outreach brief §2 | CONFIRMED |
| 20 | Calendar | `events.insert` + `conferenceDataVersion=1` creates Meet links; `freebusy.query` | see outreach brief §4 | CONFIRMED |
| 21 | CAN-SPAM | Applies to B2B; postal address, opt-out, honor within 10 business days, no deceptive headers/subjects | FTC CAN-SPAM compliance guide (outreach brief §5) | CONFIRMED |
| 22 | TCPA | Prior express (written for marketing) consent for autodialed/prerecorded calls/texts to mobiles; revocation rules | eCFR 47 CFR 64.1200 (outreach brief §5) | CONFIRMED; one-to-one rule vacatur and revoke-all delay REPORTED |
| 23 | Texas | Bus. & Com. Code ch. 304 business exemption (§304.004(3)); ch. 302 no general B2B exemption; SB 140 (2025) texts | see outreach brief §5 | CONFIRMED / REPORTED |
| 24 | LinkedIn | User Agreement §8.2 prohibits bots/automated messaging | https://www.linkedin.com/legal/user-agreement | CONFIRMED |
| 25 | Anthropic | Model IDs, structured outputs, refusal fallbacks, pricing | Claude API skill reference bundled with Claude Code (2026-10) | CONFIRMED |
