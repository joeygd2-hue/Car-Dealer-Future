# MyBot Flipper: review and implementation plan

Prepared 2026-10-19. Status: **plan only; no Flipper code has been written.** The owner asked to finish and
stabilize the existing build first. The launch-readiness audit (`docs/audit/launch-readiness-audit.md` §7,
blocker #5) also says no new phase work should start until the diverged phase branches are one application.
Phases 1, 1.5, 2 and 4 were integrated on 2026-10-19 (`docs/progress/build-status.md`). Phase 3 still has to be
ported, and the Flipper buyer-response assistant depends on it.

Labels: **CONFIRMED** = official source checked, with link. **REPORTED** = credible secondary source.
**UNVERIFIED** = assumption still to be checked. **HYPOTHESIS** = a number to validate with real usage.

---

## 1. What already exists and can be reused

Flipper is meant to be the same app as the dealer product, with a simpler surface and lower limits. It reuses
the existing models. It does not get its own application or duplicate tables.

| Need | Existing component | Reuse | Gap |
|---|---|---|---|
| Account / tenant | `Client` (`models_ops.py`), client users (`role="client"`, `client_role`), `dealer_ctx` tenant isolation, composite tenant FKs | A flipper is a `Client` with one `dealer_admin` user | `Client.account_type` (`dealer`/`individual`) + plan; self sign-up (today staff create clients) |
| Vehicle record | `Vehicle` (year/make/model/trim, VIN, mileage, price, condition, title_status, features, location) with `InventorySource.source_type="manual"` and manual entry form | Directly | Disclosure fields: known defects, accident disclosure (seller-stated), modifications, maintenance records, "verified by" per item. Add a `disclosures` JSON (or columns) on `Vehicle` rather than a new table |
| VIN checks | `services/inventory/vin.py` (check digit, model-year cross-check) | Directly | Decoding (NHTSA vPIC), see §3 |
| Photos | `VehiclePhoto` (URL + position); Phase 4 `Attachment` uploads (type/size validation, private storage) | Store uploads as attachments, reference them from `VehiclePhoto` | Upload UI, image serving route, resizing/EXIF-GPS stripping, storage quota |
| Listing text | `services/listings/` deterministic EN/ES generator, optional Claude generator (`structured_call`), facts snapshot, immutable revisions, human edit | Directly | Individual-seller voice and fields (no "dealership", no stock number, no financing CTA) |
| Honesty checks | `services/listings/guardrails.py` (unsupported warranty/accident/title/owner claims, mismatches, urgency, keyword stuffing, branded-title disclosure) | Directly | Required defect-disclosure section; "unverified" labels on seller-stated history |
| Marketplace | `FacebookMarketplaceManual` channel: copy package, checklist, record URL, update/withdraw flags | Directly | Checklist wording for an individual's own account; mobile copy buttons (`static/copy.js` exists) |
| Status tracking | `ChannelPublication` (manual_required → published_confirmed with URL, needs_update, withdrawal_required) | Directly | None for MVP |
| Buyer replies | Phase 1 rule classifier (EN/ES), Phase 3 customer-response engine (`services/conversations/`, staff-assisted Marketplace intake) | Directly (ported 2026-10-19) | Paste-in conversation for Marketplace (no API), scam/safety escalation rules for private sales |
| Cost accounting | `cost_entries` per client with measured/estimated basis, `llm.complete` token recording, budgets + alerts | Directly, per user | `structured_call` (listing generator) does not record usage yet; per-plan quotas |
| Billing | Stripe webhook (Phase 4, receives events only) | Later | Subscriptions/Checkout not built. Deliberately deferred (§7) |

## 2. Integrations: real vs mocked today

| Integration | State |
|---|---|
| Email, calendar, Zoom, Twilio, Gmail | Code + mocked HTTP tests only; never called live |
| Meta vehicle catalog (Phase 2) | Guarded code, mocked; not live-verified; dealer ads only, not relevant to individuals |
| Facebook Marketplace | Manual package only (no API exists, see §5) |
| Claude API | Off by default; stubbed tests only; no live call made |
| Stripe | Webhook verification with self-signed payloads only |
| NHTSA vPIC VIN decode | Researched, **not implemented** |
| Comparable listings / valuations | **Nothing implemented**; no source licensed |
| Inventory feeds | Real parsing code; tested with fixtures and mocked HTTP |

## 3. Vehicle identification and research

**VIN decoding: NHTSA vPIC.** CONFIRMED: free, no key, single and batch (≤50) decode
(`DecodeVinValues/{VIN}?format=json&modelyear=YYYY`). Traffic is rate-controlled with no published threshold, so
we should cache decodes per VIN and call it only when the user asks. Use the decode to **suggest** year, make,
model, trim, body, engine and drivetrain. The seller confirms each value, and a mismatch with what they entered
is shown as a question, never overwritten. vPIC decodes factory specifications only. It knows nothing about
history, ownership, accidents or condition, and the UI must say so.

**Comparable active listings and price range.** Scraping Marketplace, Craigslist, Autotrader, CarGurus etc. is
ruled out (terms; Meta's automated-collection terms CONFIRMED in the Phase 2 research). Licensed options found:

| Source | What it gives | Cost (as published, verify before buying) | Status |
|---|---|---|---|
| MarketCheck Price API | Predicted price; Premium tiers add similar/active comparables (price, miles, days on market) | US used: Base $0.07, Premium $0.09, Premium Plus $0.13 per call | REPORTED from vendor pages ([pricing](https://marketcheck.com/pricing), [APIs](https://www.marketcheck.com/apis.md)) |
| VinAudit Market Values API | Market value from aggregated private-party and dealer listings | 100 free trial queries; $100/month + $0.10/query; enterprise on request | REPORTED ([vendor page](https://data.vinaudit.com/market-values-api)) |
| Edmunds TMV | Dealer retail / private party / trade-in by ZIP | Plans and licence terms not public | REPORTED ([docs](https://developer.edmunds.com/api-documentation/vehicle/price_tmv/v2/)); availability UNVERIFIED |
| Black Book, KBB | Valuation APIs exist; no public terms or private-party US product found | Quote-based | UNVERIFIED |

Before any purchase, confirm in writing: whether we may show comparables to end users, how long we may store
them (retention), rate limits, and whether vendor accuracy claims (MarketCheck says "within 5%") hold up. Those
are vendor statements, not independent results.

**Fallback that costs nothing:** the seller pastes up to 5 comparable listings they found themselves (price,
miles, trim, location, URL). These are labelled "seller-supplied, not verified". Both paths feed one
explanation: how each comparable differs (miles, trim, condition, distance), a recommended **asking-price
range** rather than a single number, how many comparables it rests on, and its limits ("active asking prices,
not sale prices; condition not inspected"). Below 3 comparables, show no range.

**Never fabricated:** history, ownership, mechanical condition, accident history, sale/transaction prices.
Seller-stated facts are shown as "seller-stated"; anything unconfirmed is "unknown". The listing guardrails
already block unsupported accident/title/owner/warranty claims. Flipper extends them with a required defects
section and the seller-stated labels.

**Missing-information prompts:** `facts["missing"]` already lists empty fields. Flipper turns them into
questions (title in hand and in your name? accident history known? last service? tire condition? smoke/pets?),
and none of them is skipped silently.

## 4. Listing generation

Flipper reuses the generator and guardrails. It adds:
* **Title:** year, make, model, trim and one or two verified differentiators (mileage, a confirmed feature), no
  emoji or all caps. Meta's catalog rules also forbid all-caps descriptions.
* **Description:** verified facts, seller-stated history (labelled), maintenance records, modifications, a
  mandatory "Known issues" section, and pickup/viewing terms. Readable length, EN and optionally ES.
* **Keywords:** short list of model/trim/body terms buyers search for; the existing keyword-stuffing guardrail
  applies.
* **Features:** only confirmed features; VIN-decoded equipment shown as "factory spec, confirm it's present".
* **Price:** seller's asking price, plus the range and rationale from §3 when available.
* **Photo order and gaps:** a fixed checklist (front ¾, rear ¾, both sides, interior front/rear, dashboard with
  odometer reading, engine bay, tires/tread, title-status-relevant docs **with VIN/personal data redacted**,
  each disclosed defect). The seller tags each photo, and missing tags become suggestions. No image analysis in
  the MVP. Photo classification with Claude vision is a later option, costed in §7.
* Everything is editable before publication. Edits create a `human_edit` revision, and the guardrails re-run
  on the edited text.

## 5. Facebook Marketplace: what's verified

* **No API for individual or vehicle listings.** CONFIRMED in the Phase 2 research (partner vehicle catalogs
  ended 2021-09-13; Page vehicle listings ended 2023-01-30). Re-checked 2026-10-19: Meta's Marketplace
  Partnership Program and approval APIs are for approved commerce partners, not individual sellers
  ([Marketplace Partnerships](https://developers.secure.facebook.com/docs/marketplace/partnerships/)). Meta's
  standalone Marketplace seller app (released 2026-07-24, Android testing) offers no developer access that we
  found (REPORTED: [ppc.land](https://ppc.land/marketplace-sellers-gain-a-separate-meta-app-as-listings-hit-430m-a-month/),
  [EcomCrew](https://www.ecomcrew.com/meta-just-gave-facebook-marketplace-sellers-their-own-app/)).
* **No API for Marketplace buyer messages.** REPORTED. Messenger Platform covers Pages only.
* **Browser automation is excluded.** Meta's Terms prohibit automated access without permission (CONFIRMED). We
  never ask for, store or use a user's Facebook credentials.

So Flipper ships the manual workflow, which Phase 2 has already built: a final package, ordered photo download,
one-tap copy of title/description/price, a checklist, status tracking, and the published URL (validated against
`facebook.com/marketplace/item/…`). Buyer messages are pasted into MyBot for drafting, and the seller copies the
approved reply back. Re-check Meta's developer docs before each milestone. A future official API would be
implemented only through that API, with the user's explicit authorization.

## 6. Buyer response assistant

Phase 3's customer-response engine (classification, approved answers, escalation, send gate) gets reused once
ported. Flipper-specific parts:

* **Input:** the seller pastes a buyer message (no inbox integration exists for Marketplace).
* **Intents:** availability, mileage, features, price/negotiation, location, viewing request, history questions.
* **Drafts from verified listing data only.** Unknowns are answered as unknown ("I don't have service records
  for that").
* **Never drafted as fact without the seller's input:** that the car is available (the seller confirms at draft
  time), agreeing to a price, payment received, an inspection or test drive booked. Price replies offer the
  seller's own stated floor only if the seller set one, and they always require approval.
* **Escalate and warn, never auto-reply:** overpayment or refund requests; shipping/escrow "agents"; gift
  cards, crypto, wire or payment-app pressure; requests for verification codes (Google Voice / SMS codes);
  requests for the VIN plus personal details or title photos; deposits before viewing; out-of-area "buy unseen"
  pressure; threats, harassment, or meeting-safety concerns (suggest public, daytime, bring someone). Legal or
  title questions go to the seller.
* **Approval:** every message is a draft the seller copies or approves. No auto-send mode exists, and none
  would ship without a separately authorized, tested integration.

## 7. Costs, limits and tiers

**Unit costs (estimates; measure in the pilot).** Model prices CONFIRMED (Anthropic price list, cached
2026-10-06): Opus 5.5 $4 / $20 per million input/output tokens; Sonnet 5.5 $2 / $10; Haiku 5.5 $0.10 / $0.50.

| Action | Assumption | Cost per action |
|---|---|---|
| Listing draft, template generator | No AI | $0 |
| Listing draft, Claude (Opus 5.5, effort medium) | ~3k input, ~2.5k output incl. thinking | ≈ $0.06 |
| Same on Haiku 5.5 (needs a quality eval first) | Same tokens | ≈ $0.002 |
| Buyer reply draft (Opus 5.5) | ~1.5k input, ~0.4k output | ≈ $0.014 |
| Photo tagging with vision (later) | 10 photos ≈ 15k input tokens | ≈ $0.06 Opus / $0.002 Haiku |
| VIN decode (NHTSA) | Cached per VIN | $0 |
| Comparables / value | MarketCheck Premium Plus or VinAudit per query | $0.10–0.13 (VinAudit also has a $100/month minimum) |
| Photo storage | 12 photos × ~1.5 MB, object storage | < $0.01 per listing per month |
| Card payment fee | 2.9% + $0.30 typical | $0.56 on $9; $0.85 on $19 |

**Example month (HYPOTHESIS):** a Flipper user with 3 listings, 3 AI drafts + 3 regenerations, 3 comps
queries and 40 buyer replies costs ≈ $0.36 + $0.39 + $0.56 ≈ **$1.30** in AI/data, plus payment fees and a
share of hosting. The main cost risks are comps fees (especially a fixed vendor minimum) and unlimited
regeneration, so both need hard caps. Offer no unlimited plan until pilot usage is measured (`cost_entries`
already records per-client usage; `structured_call` must start recording too).

**Tiers (feature boundaries; prices are hypotheses):**

| Tier | For | Listings/month | AI drafts | Comps queries | Buyer replies | Price hypothesis |
|---|---|---|---|---|---|---|
| Free | Trying it | 1 active | Template only + 2 AI drafts | 0 (seller-supplied comps only) | 10 | $0 |
| Flipper | Occasional seller | 3 active | 15 | 3 | 100 | $9–12/month |
| Active Flipper | Regular flipper | 10 active | 60 | 15 | 400 | $19–29/month |
| Dealer | Independent dealership | Inventory feed | Bilingual, batch | Per contract | Phase 3 channels | Existing service packages |
| Pro | Multi-user, integrations | Unlimited by contract | By contract | By contract | By contract | Quote |

Billing is **not** built now. First comes a pilot with staff-created individual accounts and limits enforced
in code. Next, measure cost per active user for 4–6 weeks. Only then add Stripe Checkout/subscriptions on top
of the existing webhook. If a seller sells often, the Texas dealer-licence rule (§8) means "Active Flipper"
users may need to become Dealer customers.

## 8. Platform and legal restrictions to respect

* **Meta:** no automation, no credential collection, Commerce Policies (Marketplace is for individuals;
  prohibited items), listing must match the vehicle. CONFIRMED (Phase 2 research).
* **Texas dealer licensing:** selling 5 or more vehicles in 12 months reportedly requires a GDN dealer licence
  (UNVERIFIED; check TxDMV). Selling a vehicle not titled in the seller's name ("title jumping",
  "curbstoning") is unlawful in Texas (UNVERIFIED wording). Flipper should show a plain notice and count a
  user's sales, without giving legal advice. Other states differ.
* **Disclosure and consumer protection:** never overstate condition; the guardrails and "seller-stated" labels
  exist for this. The FTC Used Car Rule's Buyers Guide applies to dealers, not private sellers (UNVERIFIED for
  our exact case). Attorney review before launch, as for the dealer product.
* **Data:** VINs, photos of titles and buyer messages are personal data. Strip photo GPS metadata, set
  retention for pasted messages (Phase 4 retention job exists), never store payment details (PAN rejection
  exists).
* **Data vendors:** follow each comps licence (display, storage, retention, rate limits). Don't cache beyond
  the licence.

## 9. Implementation plan and acceptance criteria

**M0: finish the existing build (prerequisite, next).**
* ~~Port Phase 3 into dealerflow, replacing the interim conversation tables.~~ Done on
  `claude/gifted-phase3-integration` (one auth system, one send gate); see build status.
* Merge the integration branch to `main`; build the Docker image once; verify one live integration with test
  accounts.

**M1: Flipper MVP (manual, low-cost; invite-only pilot).**
* `Client.account_type="individual"`; staff (or an invite link) can create an individual account with one
  `dealer_admin` user. No public sign-up yet.
* Mobile-first "Sell a car" flow (≤ 5 screens): vehicle basics, mileage and price, condition and known defects,
  title status and seller-stated history, features/mods/records, photos (upload, reorder, tag).
* Listing generated with the template generator (Claude optional behind the flag). Guardrails require a known-
  issues section and label seller-stated history. All text is editable, and edits are re-checked.
* Marketplace package with copy buttons, checklist, status and URL tracking, reusing `ChannelPublication`.
* Per-plan limits enforced; per-user usage visible to staff; listing-generator usage recorded in `cost_entries`.
* Acceptance: an individual user cannot see any other client's data (existing isolation tests extended);
  a listing cannot be approved without a defects section (even "none known") and photos; no request
  leaves the server except optional Claude calls; the flow works at 375 px width (headless-Chromium check);
  every page renders for the individual role.

**M2: VIN decode and missing-info prompts.**
* NHTSA vPIC client with caching, timeout and graceful failure (manual entry always works). Decoded values are
  suggestions with "factory spec" labels. Mocked-HTTP tests; one live call only with the owner's approval.
* Acceptance: decode never overwrites seller input; outage shows "decode unavailable"; history/condition
  never inferred.

**M3: Buyer response assistant (paste-in).**
* On the ported Phase 3 engine: intents, drafts from listing facts, escalation rules in §6.
* Acceptance: evaluation set of ≥ 60 synthetic buyer messages (EN/ES, including ≥ 15 scam/safety cases):
  0 drafts promising availability, price, payment or appointments without seller input; 100% of scam cases
  escalated; seller approval required for every draft.

**M4: Comparables and price range (needs owner approval to purchase a data licence).**
* Seller-supplied comps first (no cost), then one licensed provider after a written licence review.
* Acceptance: range shown only with ≥ 3 comparables; each comparable's differences explained; limitations
  text always present; provider responses retained only as the licence allows; per-plan query caps enforced.

**M5: Billing (after 4–6 weeks of measured pilot costs).**
* Stripe Checkout + subscriptions in test mode first, plan changes, quota reset, failed-payment handling via
  the existing webhook.

## 10. Decisions needed from the owner

1. Approve porting Phase 3 (M0) before any Flipper code.
2. Pilot scope: invite-only individual accounts in Texas first?
3. Whether to request trial access/licence terms from MarketCheck or VinAudit (no purchase without your
   approval), or launch M4 with seller-supplied comps only.
4. Claude on by default for Flipper drafts (≈ $0.06 each on Opus 5.5) or template-first with Claude as an
   upgrade; and whether to run an eval comparing Haiku 5.5 for drafts.
5. Attorney review of the Texas licensing notice, disclosures and buyer-safety wording.
