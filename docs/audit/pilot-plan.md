# Controlled pilot plan (draft: **requires your explicit approval before any contact**)

Status: **Not started.** Nothing in this plan has been sent, called or booked. Every number below is
a plan or a hypothesis, not a result.

## 1. What we offer

**Service:** "Bilingual internet-lead follow-up for independent used-car dealers." Every web, marketplace or
Facebook lead gets a reply within minutes, in English or Spanish, then follow-ups until a person at the
dealership takes over. A weekly report shows lead counts and response times.

⚠️ **Delivery dependency:** the lead-response product belongs to later phases (the Phase 3 "customer
response" work currently sits on a separate branch with its own codebase). During the pilot it would be
delivered **concierge-style** (set up and run by you with existing tools), or the pilot waits until Phase 3 is
merged onto this platform. Don't promise automation that isn't running.

## 2. Buyer and problem (hypotheses to test)

| | Hypothesis | How the pilot tests it |
|---|---|---|
| Buyer | The owner or general manager of an independent lot (often the same person) in Fort Bend County | Who replies, and who books the meeting |
| Problem | Internet leads come in after hours or while staff are busy; slow or English-only replies lose Spanish-speaking buyers | Meeting discovery questions: lead volume, current response time, language mix (record what they *say*; don't assume) |
| Value | Faster first response and consistent follow-up mean more appointments from the same leads | Not measurable until a client goes live; the pilot only measures interest |

## 3. Pricing hypothesis (unvalidated: no market research done)

| Option | Setup | Monthly | Purpose |
|---|---|---|---|
| A | $0 | $299 after a 30-day trial | Tests low-friction adoption |
| B | $250 | $499 | Tests willingness to pay for done-for-you setup |

Mention pricing only in meetings, not in cold emails. Record each prospect's reaction in the proposal notes.
Treat these as hypotheses; change them after 5 conversations.

## 4. Who we contact

1. Source: TxDMV Independent (GDN) list, Fort Bend County, active Motor Vehicle licenses, franchises excluded (191 dealers as of 2026-10-18).
2. Wave 1: the **20 highest-scoring** of the 33 dealers with a business-domain email, in Sugar Land, Stafford, Missouri City, Richmond and Rosenberg.
3. Before drafting, a person spends about 5 minutes per dealer:
   - Open the dealer's website.
   - Mark the dealership type verified.
   - Mark whether Spanish content is present, with a verified observation if useful.
   - Add the owner or GM as a verified contact only if published.
4. Exclude any dealer with a complaint history you're aware of, existing customers, and anyone on the suppression list.

## 5. Approval process (every message)

- Agent **stopped**. Campaign "Sugar Land intro" in **manual** approval mode. Live operation authorized only for the pilot window.
- The owner (manager/admin role) reads and approves **each** initial message and each follow-up individually in the outreach queue.
- Templates (EN/ES) and the footer postal address are reviewed by an attorney before wave 1 (compliance checklist E4).
- Replies are answered by a person. No auto-answers during the pilot. Meetings are booked only after you approve each one.
- No phone calls in wave 1 (Chapter 302 question open). Inbound calls from interested dealers are fine.

## 6. Volume and budget limits

| Limit | Value | Enforced by |
|---|---|---|
| New dealers emailed | 20 (wave 1). A wave 2 of 20 needs your approval | You |
| Emails per day | ≤ 5 | `DF_OUTREACH_DAILY_CAP=5` |
| Touches per dealer | ≤ 2 (intro + one follow-up after 5 business days) | `DF_OUTREACH_MAX_TOUCHES_PER_PROSPECT=2` |
| Cooldown | 5 days | `DF_OUTREACH_PER_PROSPECT_COOLDOWN_DAYS=5` |
| Pilot duration | 4 weeks | Calendar |
| Money | ≤ $100 total without further approval (domain/DNS or tools). No paid data, ads or Twilio | Agent budgets: daily $5, monthly $100 |

## 7. Stop conditions (pause everything with the Agent page **EMERGENCY STOP**, then review)

| Signal | Threshold | Action |
|---|---|---|
| Spam complaint or abuse report (any channel) | **1** | Stop the pilot; review list and copy |
| Hard bounces | ≥ 2 in the first 10 sent, or > 5% overall | Pause; verify addresses before continuing |
| Opt-outs | ≥ 3 of the first 20 delivered (15%) | Pause; revise targeting or copy |
| Gmail Postmaster spam rate (if shown) | ≥ 0.1% | Stop |
| Hostile or legal-sounding reply | 1 | Pause; involve counsel |
| Any message sent that you didn't approve | 1 | Emergency stop; investigate the audit log |
| SPF, DKIM or DMARC failing on test sends | any | Don't start |

## 8. Metrics (real results only; simulated and live-check activity excluded automatically)

| Metric | Definition | Where |
|---|---|---|
| Sent | Messages a real provider accepted | Weekly report: "Contacted (actually sent)" |
| Delivered | Sent minus bounces within 72 h | Sent − bounced |
| Reply rate | Dealers who replied (not auto-replies) ÷ delivered | Weekly report |
| Positive reply rate | Replies classified interested, meeting_request, question or referral (corrected by a person) ÷ delivered | Inbox (reclassify if wrong) |
| Opt-out rate | Opt-outs ÷ delivered | Dashboard |
| Qualified meeting | Booked, provider-confirmed meeting with the owner/GM | Meetings page |
| Attendance | Meetings marked held ÷ booked | Meetings page |
| Conversion | Won (paid or signed trial) ÷ meetings held | Pipeline |
| Cost per meeting | Recorded actual costs ÷ qualified meetings | Weekly report |

**Decision targets (hypotheses):** reply rate ≥ 10%, positive ≥ 5%, at least 2 qualified meetings and 1 signed trial from 20 dealers.
Below that after wave 1: change the offer or channel before scaling.

## 9. Before the pilot can start (all required)

1. Resolve the production blockers in `launch-readiness-audit.md` (secrets handling, HTTPS host, backups, MFA proxy).
2. Complete the live verification steps in `integration-setup.md` §7, with results recorded.
3. Attorney review of templates, footer and the Chapter 302 question.
4. Decide how the service will be delivered to the first client (concierge or Phase 3).
5. **Your written go-ahead** for wave 1 (dealer list, templates and dates).
