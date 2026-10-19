# Research Brief: Publishing Vehicle Listings for Used-Car Dealerships (Meta and other channels)

Prepared 2026-10-18. Labels: **CONFIRMED** means an official doc was fetched or seen, with URL. **REPORTED** means a credible third party, with URL. **UNVERIFIED** means an assumption or something not checked. Most Meta developer and help pages show no visible date. Where a date was visible, it is noted.

Caveat on sources: Meta Business Help Center pages (facebook.com/business/help/...) render client-side, so WebFetch got only their titles, or a 404. Claims tied to those pages come from the search-result text Meta's own pages produced, and are marked as such.

---

## 1. Can a third-party vendor publish individual vehicle listings to Facebook Marketplace via API (US, 2026)?

**Answer: No. There is no sanctioned API or partner route for organic (free) Marketplace vehicle listings by a dealer or vendor.**

History:
- **Pre-2021:** Meta ran a Marketplace vehicle partner program. Dealer inventory feeds from approved partners appeared as organic Marketplace listings. Reported partners included Auction123, Cars.com, CDK Global, Edmunds and SOCIALDEALER. REPORTED, per a summary of the trade press ([dealerteamwork.com](https://dealerteamwork.com/facebook-marketplace-changed-here-are-4-things-to-do-about-it/)). The vehicle catalog field reference still has leftover Marketplace rules: mileage over 500, at least 2 images, a Call button tied to `dealer_phone`, and an `fb_page_id` "allow-listed" requirement. CONFIRMED that these remnants exist ([auto-ads reference](https://developers.facebook.com/documentation/ads-commerce/marketing-api/auto-ads/reference)).
- **13 Sept 2021: partner catalog distribution ended.** Meta's help article "Vehicle and home listings from partner catalogues discontinued on Facebook Marketplace" says that from 13 Sept 2021, Marketplace stopped distributing vehicle, home-rental and home-sale listings from partner catalog feeds. The countries affected are the US, Canada, the UK, Australia, Brazil, France, Germany, Indonesia and Mexico. Automotive Inventory Ads continue to display on Marketplace. CONFIRMED from the official page title and its search-result text ([en-gb.facebook.com/business/help/442276120199473](https://en-gb.facebook.com/business/help/442276120199473)); a direct fetch returned 404. The May 2021 notice is reproduced in [DealerRefresh forum](https://forum.dealerrefresh.com/threads/vehicle-listings-from-partner-catalogs-will-be-discontinued-on-facebook-marketplace.7494/) (REPORTED). DealerCenter told its dealers that they "will no longer be able to use a software system or service to bulk-upload their used inventory" ([DealerCenter blog](https://dcnation.dealercenter.com/blog/important-news-about-facebook-marketplace), REPORTED). The old help article "create or manage dealership vehicle listings with a partner catalog" (business/help/192241165780348) now resolves to a generic "Create a Catalog in Commerce Manager" page. That fits retirement (CONFIRMED, by title only).
- **30 Jan 2023: business-Page vehicle listings ended.** A Meta email said: "Starting on January 30, 2023, Meta will no longer support the ability for sellers to create vehicle & real estate" listings from Pages, and "Your ability to reach audiences via ads is also not changing." The Vehicles and Manage Inventory tabs were removed. REPORTED ([Jalopnik, 30 Sep 2022](https://www.jalopnik.com/facebook-will-no-longer-let-dealers-list-cars-suggests-1849603029/)).
- **2024–Oct 2026:** Searches found no reinstatement or new partner program (UNVERIFIED that none exists, since absence is hard to prove). A vendor page dated 1 Oct 2026 says the only free route left is a person posting manually from a personal profile with Facebook's own form. It also says the tools now on the market are browser extensions driven by the salesperson's own login ([turbosell.co, Oct 2026](https://turbosell.co/blog/dealer-website-platforms-facebook-marketplace), REPORTED, vendor marketing).
- **Risk of browser-automation posting:** Meta's Terms say "You may not access or collect data from our Products using automated means (without our prior permission)," whether logged in or not (CONFIRMED, [facebook.com/terms.php](https://www.facebook.com/terms.php), effective 1 Jan 2025). The Automated Data Collection Terms require "Meta's express written permission" for collection with bots or scrapers (CONFIRMED, [automated_data_collection_terms](https://www.facebook.com/legal/automated_data_collection_terms), effective 7 Oct 2024). These terms are written mainly about *collecting* data. Whether automated *posting* breaches them is a legal question. Account restriction is a realistic risk (ASSUMPTION). The Commerce Policies also say Marketplace is meant for individual sellers, not businesses (CONFIRMED, [policies_center/commerce](https://www.facebook.com/policies_center/commerce)).

**Product implication:** Treat Marketplace as **"prepare and copy for a human to post manually"**, plus optional paid Automotive Inventory Ads, which can appear in the Marketplace placement. Do not automate posting.

## 2. Meta vehicle catalogs and Automotive Inventory Ads (AIA)

- **What they power:** Vehicle catalogs power **ads**: Automotive Inventory Ads and Inventory Offer (model) ads. Since 2021 they produce no organic Marketplace listings. AIA "continue to be displayed on Marketplace and across other placements" as paid ads (CONFIRMED via the Meta help article text above). An on-Facebook destination for AIA, a vehicle detail page, also exists (REPORTED, via the 2021 notice links).
- **Catalog type:** Automotive (Vehicles) catalog. Feed formats are Inventory Ads (Vehicle, Dealership) and Inventory Offers. Sample feeds come as CSV, TSV or XML (CONFIRMED, [auto-catalog](https://developers.facebook.com/docs/marketing-api/dynamic-ads-auto/auto-catalog)). Feeds can be uploaded in Commerce Manager, sent through a third-party provider (Meta names Sincro and Naked Lime) or entered manually. Meta suggests manual entry for dealers with fewer than 50 vehicles (CONFIRMED via search text, [business/help/143781049600895](https://www.facebook.com/business/help/143781049600895)).
- **Vehicle fields** (CONFIRMED, [auto-ads reference](https://developers.facebook.com/documentation/ads-commerce/marketing-api/auto-ads/reference)):
  - **Required:** `vehicle_id` (max 100 characters), `make`, `model`, `year`, `mileage.value`, `mileage.unit` (MI/KM), `price` (e.g. "18000 USD"), `state_of_vehicle` (New/Used/CPO), `image[0].url` (up to 20 images, 4 MB each), `address`, `body_style` (enum), `exterior_color`, `url`, `title` (max 500), `description` (max 5,000, no links or all caps).
  - **Optional:** `vin` (17 characters), `availability` (available/not_available), `stock_number`, `dealer_id`, `dealer_name`, `dealer_phone`, `custom_label_0–2`, `status` (active/archived).
  - `fb_page_id` is required only for the on-Facebook destination and needs allow-listing.
- **Batch API:** `POST /{catalog_id}/items_batch`, with `item_type` values that include `VEHICLE` and `VEHICLE_OFFER`. Methods are CREATE, UPDATE and DELETE, and they can be mixed in one call. A call takes up to 5,000 requests (under 3,000 recommended) and at most 28 MB. `allow_upsert` defaults to true. The call returns a handle, which you poll with `GET /{catalog_id}/check_batch_request_status`. Processing is asynchronous. CONFIRMED ([items_batch reference](https://developers.facebook.com/docs/marketing-api/reference/product-catalog/items_batch/), [catalog-batch guide](https://developers.facebook.com/docs/marketing-api/catalog-batch)). The legacy `/batch` endpoint is limited to commerce catalogs, and Meta says it should get no new integrations.
- **Rate limits** (CONFIRMED, [rate-limiting](https://developers.facebook.com/docs/graph-api/overview/rate-limiting)):
  - Catalog Batch: 8 + 8·log2(DA impressions + PDP visits) calls per minute, per catalog.
  - Catalog Management: 20,000 + 20,000·log2(...) calls per hour.
  - So a new, low-traffic catalog gets about 8 batch calls per minute. At 5,000 items per call, that is ample for dealer-sized inventory (ASSUMPTION).
- **Permissions:** `catalog_management` lets an app create, read, update and delete business-owned catalogs. It depends on `business_management`, and App Review needs a use-case description and a screencast of create, update and delete (CONFIRMED, [catalog_management](https://developers.facebook.com/docs/permissions/reference/catalog_management)). "Business Verification is required for all apps making requests for Advanced Access," and managing other businesses' assets requires App Review (CONFIRMED, same page). Creating ads also needs `ads_management` (UNVERIFIED for AIA specifically; it is the standard Marketing API permission).
- **Cost:** No API fee is documented. Spend comes from ad budgets (ASSUMPTION, typical of Meta). AIA are paid placements (CONFIRMED in the sense that they are ads).

## 3. Messaging (for Phase 3 handoff)

- Meta's API docs have no documented way to read or answer **Marketplace** buyer messages. Marketplace chats are tied to the seller's personal profile. The Messenger Platform works only for **Pages**. REPORTED ([ManyChat community](https://community.manychat.com/general-q-a-43/bot-in-facebook-marketplace-401)). No official page says this either way (UNVERIFIED).
- `pages_messaging` lets an app manage Page conversations in Messenger. It depends on `pages_manage_metadata` and `pages_show_list`, and App Review needs a screencast plus a cURL send example (CONFIRMED, [pages_messaging](https://developers.facebook.com/docs/permissions/reference/pages_messaging)).
- **Messaging window:** There is a 24-hour standard window after a user interaction. The HUMAN_AGENT tag allows manual replies for up to 7 days. Other message tags cover approved use cases only (CONFIRMED, [policy overview](https://developers.facebook.com/docs/messenger-platform/policy/policy-overview)).
- **Implication:** AIA with a Messenger (CHAT) destination, using `dealer_communication_channel=CHAT`, routes leads to the dealer's Page inbox. That inbox *is* reachable through the API. Organic Marketplace chats are not, so they need a human handoff.

## 4. Policies

- Commerce Policies ban the sale of "certain vehicle parts and accessories" and say Marketplace is for individuals. In the EEA, the Philippines and India, businesses can lose Marketplace access (CONFIRMED, [policies_center/commerce](https://www.facebook.com/policies_center/commerce)). The fetched page had no vehicle-specific rules, such as the seller owning the vehicle.
- See Section 1 for the automated-access prohibitions in the Terms and the Automated Data Collection Terms.

## 5. Dealer inventory sources, VIN decoding, Google

- **DMS and syndicator pattern:** Inventory usually leaves the dealer's DMS or inventory tool as a scheduled **FTP/SFTP CSV** export, mapped per destination. The dealer requests it, and the destination must already have an account (REPORTED: [DealerRefresh thread](https://forum.dealerrefresh.com/threads/first-post-i-am-looking-for-information-regarding-inventory-feeds-to-power-an-automotive-retailing-site-like-autotrader-or-cars-com.7213/); [dealr.cloud docs](https://dealr.cloud/docs/docs/inventory/advertising/), which show "feed setup request" emails to CarGurus, Cars.com, AutoTrader, TrueCar, CARFAX and Carsforsale, with dealer IDs for some). **For a new vendor, the realistic path is to become a feed destination: host an SFTP drop and ask dealers or their providers to add you as an export.** (ASSUMPTION, standard industry practice.)
- **Frazer:** Has no public API. Integrations are limited to pre-approved partners, so manual export or file drops are the fallback (REPORTED, [supergood.ai](https://supergood.ai/api-report-card/frazer)).
- **DealerCenter:** Users describe it as hard to integrate or export from (REPORTED, forum).
- **Wayne Reaves:** Spelling is uncertain. No export documentation was found (UNVERIFIED).
- **AutoManager, Dealer.com, DealerInspire, Dealer Car Search:** Not researched (UNVERIFIED). They are assumed to support third-party FTP exports on dealer request.
- **Dealertrack DMS:** No public API. Access goes through the **Opentrack** certification program, with a signed agreement and certification at Dealertrack's discretion (REPORTED, [supergood.ai](https://supergood.ai/api-report-card/dealertrack-dms); [BusinessWire 2024](https://www.businesswire.com/news/home/20240426107793/en/Motive-Retails-Motive-Integrator-eXchange-MIX-Announces-Certification-with-Cox-Automotive-Dealertracks-Opentrack-Integration-Program)).
- **HomeNet and vAuto (both Cox Automotive):** These syndicators push FTP/SFTP exports, reportedly every 2–4 hours, with field conversions. A dealer reportedly uses one syndicator (REPORTED, [DealerRefresh](https://forum.dealerrefresh.com/threads/homenet-vs-vauto-as-an-inventory-provider.9762/page-4), [files.com](https://www.files.com/use-with/homenet)).
- **Dealer Specialties:** Listed as a feed source by Fullpath (REPORTED, [Fullpath](https://help.fullpath.com/hc/en-us/articles/360047311611)).
- **Pricing for any of these exports:** Not public. One anecdote cites a $40/month portal (REPORTED, forum).
- **NHTSA vPIC (CONFIRMED, [vpic.nhtsa.dot.gov/api](https://vpic.nhtsa.dot.gov/api/)):**
  - `GET https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{VIN}?format=json&modelyear=YYYY` (also `DecodeVin`).
  - `DecodeVINValuesBatch` takes up to 50 VINs in the form "vin,year;vin,year". Partial VINs work with `*`.
  - The page lists no fee and no API-specific terms, only general NHTSA web policies. Traffic is subject to "an automated traffic rate control mechanism" with no published thresholds. Offline databases are downloadable.
  - "Free" is inferred, since US government data has no fee listed (ASSUMPTION).
- **Google vehicle ads (Merchant Center)** (CONFIRMED, [answer 11189169](https://support.google.com/merchants/answer/11189169?hl=en)):
  - Fully released in the US, Canada, Australia and Japan. Open beta in the UK, Germany, France, Italy, Spain and the Netherlands.
  - Dealers, aggregators and OEMs qualify. Private sellers and brokers do not.
  - New and used are both allowed, but only non-commercial passenger vehicles.
  - Needs Google Ads plus Merchant Center, and a Google Business Profile or store data source. Runs in Performance Max or Shopping. Vehicle feed service providers are optional.
  - Paid ads. US eligibility reportedly widened to ATVs, UTVs and campers (REPORTED, [ppc.land](https://ppc.land/google-vehicle-ads-in-the-us-now-cover-atvs-utvs-and-campers/)).

## 6. Other channels

- **Cars.com, Autotrader, CarGurus, TrueCar, Carsforsale:** All accept dealer inventory via **feeds from the dealer's inventory, website or DMS provider**, typically FTP CSV. Each requires a dealer account, which is usually paid (REPORTED, sources above). CarGurus' FAQ says its inventory comes from feeds sent by dealers' inventory or website-host partners, that it does not scrape, and that pricing is quote-based (REPORTED via search summary of CarGurus dealer FAQ; the page itself was not fetched). No public self-serve listing APIs were found for these sites (UNVERIFIED).
- **Craigslist bulk posting interface (CONFIRMED, [craigslist.org/about/bulk_posting_interface](https://www.craigslist.org/about/bulk_posting_interface)):**
  - A real API: HTTPS POST of RSS 1.0/RDF to `/bulk-rss/validate` or `/bulk-rss/post`.
  - Granted case by case to high-volume posters ("hundreds of postings per month"), in paid US categories including For Sale By Dealer.
  - Needs username, password and accountID with block credit or invoicing.
  - Returns a per-item `postedStatus` (POSTED, NOT_VALID, INSUFFICIENT_BLOCKS). Edits go through the returned manage URL, not by re-posting.
  - Fee is reportedly about $5 per dealer listing (REPORTED, [autosweet](https://www.autosweet.com/?p=51), [Auction123 blog](https://blog.auction123.com/tag/craigslist/)).

---

## Summary table

| Channel | Supported operations | Auth / permissions | Approval needed | Cost | Confirmation behavior | Status |
|---|---|---|---|---|---|---|
| FB Marketplace organic (dealer/vendor API) | None. Feed distribution ended 13 Sep 2021; Page listings ended 30 Jan 2023 | n/a | n/a | n/a | n/a | CONFIRMED (2021), REPORTED (2023) |
| FB Marketplace manual (personal profile) | Human posts via FB UI; app can only prepare copy and photos | User's own login; automation barred by Terms §3.2 | None | Free | Seen in the UI by the human | CONFIRMED (Terms); REPORTED (practice) |
| Meta vehicle catalog + AIA | items_batch CREATE/UPDATE/DELETE (VEHICLE), feeds (CSV/TSV/XML), ads incl. Marketplace placement | catalog_management + business_management; ads_management for ads | App Review, Business Verification for Advanced Access | API: no fee found; ad spend | Async handle, then check_batch_request_status | CONFIRMED (ads_management UNVERIFIED) |
| Meta Page messaging | Send/receive Page Messenger conversations (not Marketplace chats) | pages_messaging (+pages_manage_metadata, pages_show_list) | App Review | No fee found | Webhooks / send API response | CONFIRMED (Marketplace exclusion REPORTED) |
| Google vehicle ads | Vehicle feed to Merchant Center, PMax/Shopping ads | Merchant Center + Google Ads + GBP | Self-serve in US | Ad spend | Merchant Center item status | CONFIRMED |
| Cars.com / Autotrader / CarGurus / TrueCar | Full-inventory feed replace (FTP CSV via provider) | Dealer account + dealer ID; provider credentials | Dealer subscription, feed setup request | Subscription (quote-based) | Feed processing; no per-item API ack found | REPORTED |
| Craigslist bulk posting | Post/validate (RSS over HTTPS); edit via manage URL | Account creds + accountID | Case-by-case approval (high volume) | ~$5/listing reported | Per-item postedStatus | CONFIRMED (fee REPORTED) |
| DMS exports (Frazer, DealerCenter, Wayne Reaves, etc.) | Scheduled CSV/FTP export to destinations | Dealer authorizes destination | Dealer request; partner programs for API (Dealertrack Opentrack) | Not public | File drop | REPORTED / UNVERIFIED |
| HomeNet / vAuto (Cox) | FTP/SFTP syndication with field conversions | Dealer authorization | Dealer request | Not public | File drop every 2–4 h reported | REPORTED |
| NHTSA vPIC | VIN decode (single, batch of 50) | None | None | No fee listed | Synchronous JSON | CONFIRMED |
