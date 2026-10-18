# Metrics and reporting definitions

Every figure is computed from recorded rows. Where a figure cannot be measured, the app shows "not measurable"
or "not available" rather than a guess. **Actuals, contracted figures, forecasts and hypothetical scenarios are
always labelled separately.** SIMULATED records (from mock providers or simulated inquiries) are never counted
as real results.

## Finance (Finance page, `GET /api/v1/finance/report?start=&end=`)

| Metric | Definition | Basis |
|---|---|---|
| Recorded revenue | Sum of lines on **issued** (or later written-off) invoices with issue date in the period, minus credit notes issued in the period. Drafts and proposals are never revenue. | Actual (accrual) |
| Collected (gross / net) | Sum of `confirmed` payments with received date in the period; net subtracts refunds issued in the period. Unpaid invoices and `pending` payments are never cash. | Actual (cash) |
| Outstanding receivables | For issued invoices: total − confirmed payments − credits, as of the period end, with aging buckets (current, 1-30, 31-60, 61-90, 90+ days past due). | Actual |
| Written off | Balances of invoices written off in the period. | Actual |
| Recorded expenses | Sum of expenses dated in the period, by category. | Actual |
| Operating result | Cash: collected net − expenses. Accrual: recorded revenue − expenses. | Actual |
| MRR / ARR | Monthly-normalised recurring fee after discounts for agreements active on the period end; ARR = MRR × 12. | Contracted (not cash) |
| Average revenue per client | Recorded revenue ÷ clients billed in the period; also MRR ÷ active clients. | Actual / contracted |
| Retention / churn | Clients with an active recurring agreement at period start that have none at period end ÷ clients active at start. | Actual |
| Cost per acquired client | Acquisition spend (expense categories flagged `is_acquisition`: data sources, outreach, advertising by default) ÷ clients whose first agreement started in the period. | Actual; "not measurable" with 0 new clients |
| Cost per qualified lead | Acquisition spend ÷ real prospects that entered `qualified` in the period (CRM stage history). | Actual |
| Cost per booked meeting | Acquisition spend ÷ real (non-simulated) meetings booked in the period. | Actual |
| Client-level service costs | Expenses assigned to the client + usage cost entries assigned to the client (marked when they include estimates). | Actual + estimates (labelled) |
| Gross margin | (Recorded revenue − allocated direct costs) ÷ recorded revenue, **only** for clients with allocated costs. Unallocated overhead is shown separately and excluded. | Partial allocation |
| Cash-flow trend | Monthly confirmed collections − refunds vs expenses. | Actual |
| Cash-flow forecast | Next 3 months: contracted MRR in, last month's monthly-recurring expenses out. Assumptions are printed. | **Forecast** |
| Break-even | Clients needed = fixed monthly costs ÷ (price − variable cost per client), from inputs you enter (fixed costs pre-filled from the last 3 months' unallocated expenses). | **Hypothetical scenario** |

Only USD records are aggregated; records in other currencies are excluded and counted (no conversion).
CSV exports: invoices, payments, expenses, summary. Expense categories are for internal reporting and are
**not tax classifications or tax advice**.

## Usage costs (Costs page)

Each `cost_entries` row has a basis: **verified_billing** (copied from a provider billing export),
**estimated** (measured quantity × the rate in Settings → cost estimates), **recorded** (entered by a person) or
**unknown** (usage measured, no rate; cost not claimed). Totals are shown per basis and never merged. Token
counts from the Claude API are measured from the API response; their cost is an estimate until billing is
imported. Budgets (daily/monthly, optionally per category) alert at a threshold and at 100%; a spike alert fires
when today's spend exceeds N × the trailing 7-day daily average. Budgets include estimates.

## Outreach agent (Agent page, weekly report)

Defined by Phase 1 (`docs/architecture/autonomous-agent.md`, `services/reports.py`): prospects researched,
attempts, replies, positive replies, opt-outs, follow-ups due, meetings booked, errors, integration health,
estimated costs, conversion rates — real and SIMULATED counted separately.

## Client monthly report (Client reports, client portal)

A report is an immutable snapshot (`client_reports.data`) of the period. Each metric carries its source table and
a note. Clients see only reports a staff member published (`final`) or sent.

| Section | Metric | Source / condition |
|---|---|---|
| Channels | Status of each channel (inventory feed, customer messaging, Facebook Marketplace, calendar, Search Console, website analytics, report email) | `integration_connections`; anything not recorded is "not connected" |
| Inventory | Imports run; vehicles processed (created + updated); vehicles in stock at period end | inventory imports, vehicles |
| Listings | Generated; approved; publishing confirmed by a channel API; requiring manual publication | listings, publish attempts |
| Inquiries | Received on connected channels (simulated excluded and counted separately); by channel; median first-response minutes (only conversations with both timestamps); qualified leads; escalations | conversations |
| Appointments | Test-drive requests; confirmed/completed | appointments |
| SEO | Issues resolved in the period (no longer detected by a re-audit, or marked resolved); open at period end; organic clicks/impressions **only** from authorized Search Console/analytics data | seo issues, search metrics |
| Service usage | Usage and cost by provider and basis | cost entries for the client |
| Attribution | Fixed statement: the report does not measure or claim additional sales or revenue caused by our services | — |

## SEO findings

Each issue records: finding, evidence and affected URL, severity, likely impact, recommended fix, estimated
effort, responsible party (website provider / dealership / our team), status, detection type, and before/after
values. Detection is **measured** (directly observed), **heuristic** (rule-based opportunity, e.g. no Spanish
content) or **ai** (AI-generated recommendation; none are produced yet). Findings never promise rankings,
traffic or sales.
