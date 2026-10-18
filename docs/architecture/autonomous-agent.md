# Persistent autonomous outreach agent

The agent is a scheduled, bounded routine that runs inside the background worker. It is not a chat
session or an infinite loop, and it does not need a browser tab to stay open.

## Components

| Piece | Where | Role |
|---|---|---|
| Worker process | `python -m dealerflow worker` (`dealerflow/worker.py`) | Every 10 s: writes a heartbeat, enqueues the tick for the current time bucket, runs due jobs. Handles SIGTERM. |
| Job queue | `jobs` table, `dealerflow/jobs.py` | Unique `key` per job (idempotent enqueue), atomic `pending→running` claim, exponential backoff, `DeferJob`, failure hooks, stale-lock recovery after 15 min. |
| Tick | `services/agent.py::tick` | Runs a fixed list of steps, each capped by limits, each in its own savepoint. One failed step is recorded and the others still run. |
| State | `agent_state` (single row) | `status`, live authorization, heartbeat, checkpoint (per-step timestamps, inbox cursor, discovery counters), limits. |
| Run log | `agent_runs` | One row per tick: counts per step, errors, skipped reason. |
| Campaigns | `campaigns`, `enrollments` | Multi-step sequences with their own pause, daily cap, min score, cities and approval mode. |
| Approved answers | `knowledge_items` | The only text the agent may use to answer questions. Starts unapproved. |

## States

| Status | What runs |
|---|---|
| `stopped` (default) | Nothing. Human-approved sends still go out through the job queue. |
| `running` | All steps, subject to limits and the operating window. |
| `paused` | Inbox polling and follow-up reminders only, so opt-outs are still processed. No discovery, drafting or auto-approval. |
| `emergency_stopped` | Nothing. Every `approved`/`queued` message goes back to `draft`, and send jobs defer while the stop is active. Only an admin can clear it. |

Separately, **live operation** (`agent_state.live_authorized`, set by an admin who types `AUTHORIZE LIVE`)
together with `DF_OUTREACH_SENDING_ENABLED=true` is required before any non-simulated provider sends.
Until both hold, real-provider send jobs defer and campaign auto-approval is skipped.

## Tick steps (in order)

| Step | Runs while paused | Needs operating window | Does |
|---|---|---|---|
| `poll_inbox` | yes | no | Fetches new mail (cursor in checkpoint), stores it idempotently, classifies it, applies opt-outs, bounces and stop conditions. |
| `handle_replies` | no | yes | For campaigns with auto-answers on: answers routine questions from approved text only; anything sensitive, unmatched, low-confidence or without an approved answer becomes a review task. Meeting requests always go to a person. |
| `replenish` | no | no | When eligible prospects waiting < `replenish_below`, runs discovery (`discovery_source`) at most `discovery_max_runs_per_day`, within the daily and monthly budget. |
| `enrich` | no | no | Queues website research for prospects with a site and no website evidence (only if `DF_WEBSITE_RESEARCH_ENABLED`). |
| `score` | no | no | Re-scores new, changed or week-old prospects. Moves score ≥ threshold to Qualified, and those with an email to Ready for outreach. |
| `enroll` | no | no | Enrolls ready prospects into active campaigns that match min score and cities. |
| `sequences` | no | yes | For due enrollments: drafts the next step (one message in flight per enrollment, honours step delays and the campaign daily cap). In `auto` campaigns with no review flags it approves the draft as `campaign_auto`. Otherwise it waits in the approval queue. |
| `follow_ups` | yes | no | Creates tasks for "Follow up later" prospects whose date has arrived. |

Sending itself happens in `send_email` jobs, which re-run the full compliance gate (`services/compliance.py`)
immediately before calling the provider.

## Safety gates (all enforced in code and covered by tests)

- Suppression list (email, @domain, phone) and do-not-contact are checked at draft, approval and send.
  An opt-out from anyone at a dealership marks the whole dealership do-not-contact.
- Stop conditions: any human reply, a booked meeting, an opt-out or a bounce ends sequences and cancels
  unsent sequence messages.
- Limits: global daily cap (deferred, not dropped), per-prospect cooldown and touch cap, per-campaign
  daily cap, per-tick caps, HTTP-level token-bucket rate limits per provider, daily/monthly budget for paid APIs.
- Content: templates may only use whitelisted variables filled from verified data. A missing value blocks the
  draft. A free-text observation must be person-verified evidence, and its use forces human review.
  A CAN-SPAM footer (identity, postal address, ad disclosure, unsubscribe link and one-click header) is always added.
- Approvals: managers approve messages and bookings. Only admins can set a campaign to `auto`, enable
  auto-answers, approve knowledge items, authorize live operation or clear an emergency stop.
- Ambiguous sends are never retried blindly. A message left in `sending` after a crash becomes `failed`
  with "outcome unknown".
- Simulated sends never move a real prospect to Contacted.

## Going live (checklist)

1. Configure real providers and verify them on the Integrations page (**Verify credentials** sends nothing).
2. Fill in `DF_BUSINESS_NAME`, `DF_BUSINESS_POSTAL_ADDRESS`, `DF_SENDER_*` and a public HTTPS `DF_BASE_URL`.
3. Edit the templates and approved answers so every statement is true. Have a lawyer review them.
4. Send a few human-approved messages to yourself (a real prospect record with your own address).
5. Set `DF_OUTREACH_SENDING_ENABLED=true`, restart, and have an admin authorize live operation.
6. Start the agent with campaigns in `manual` approval mode. Move a campaign to `auto` only after reviewing
   a few hundred agent drafts.

## Operator dashboard

`/agent` shows status, worker heartbeat, live-authorization state, job queue, recent ticks with errors,
campaign controls, integration health, limits, and measured results (sent, replies, positive replies,
opt-outs, follow-ups due, meetings booked, costs, conversion rates). Simulated activity is excluded or labelled.
Costs include estimates, and are labelled that way.
