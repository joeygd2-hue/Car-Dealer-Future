# Service packages

The catalog is data (`service_packages`), edited by admins on the Packages page or via
`PATCH /api/v1/packages/{id}`. **No price is hardcoded anywhere in the application.** The three seeded packages
are **proposals with $0 placeholder prices and `pricing_status = proposed`** — they are not validated market
prices. Set real prices once you have them and mark the package `validated`.

| Code | Package | Includes | Seeded price |
|---|---|---|---|
| `listing_ops` | Listing Operations | Inventory imports; listing generation (EN/ES); listing review and approval; supported publication workflows; inventory synchronization | $0 setup, $0/month (placeholder) |
| `bilingual_lead_response` | Bilingual Lead Response | Connected-channel inquiry assistance; approved responses (EN/ES); lead summaries; appointment workflows; human escalation | $0 / $0 (placeholder) |
| `dealership_growth` | Dealership Growth | Everything in Listing Operations and Bilingual Lead Response (via `includes_packages`), plus SEO audits and reports, and monthly performance reporting | $0 / $0 (placeholder) |

## Client-specific agreements

An agreement (`client_agreements`) is created per client, optionally from a package. It **copies** the setup
fee, recurring fee, billing frequency and features at creation, so later catalog edits never change what a
client signed. Each agreement supports:

* setup fee (charged on the first invoice for the agreement),
* recurring fee with billing frequency `monthly`, `quarterly`, `annual` or `one_time`,
* a percentage discount and/or a fixed discount per billing period,
* custom terms (free text), start and optional end date,
* status `pending → active → cancelled/ended/paused`, with the cancellation date and reason recorded.

"Draft this month's invoice" on the client page creates a draft invoice for one billing period (setup fee on the
first invoice, recurring line with the service period, discount line). It refuses to invoice the same period
twice. Invoices must then be reviewed and issued.

## What a package does not do

Recording an agreement does **not** switch on automation. Each automation (inventory sync, listing publication,
automatic inquiry responses, scheduled SEO audits, monthly report emails) is enabled separately per client and
only after its onboarding prerequisites and integrations are complete (`services/onboarding.py`).
