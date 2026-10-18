# Compliance checklist: federal and Texas

Prepared 2026-10-18 as an engineering checklist. **This is not legal advice, and the application is
not legally certified.** Items marked ⚖️ need review by a qualified Texas attorney before the related
activity starts. Source IDs refer to `docs/research/source-register.md`.

Status key: **Verified** = enforced in code and covered by an automated test. **Partially verified** =
enforced in code, but depends on configuration or on behaviour not yet tested against a live provider.
**Unverified** = depends on the operator or is not yet checked. **Blocked** = can't proceed until resolved.

## A. Commercial email: CAN-SPAM Act (applies to B2B) [S-CANSPAM]

| # | Requirement | How it's met | Status | Evidence |
|---|---|---|---|---|
| A1 | No false or misleading header information | Sent from the operator's own mailbox and identity; sending blocked if `DF_SENDER_*`/`DF_BUSINESS_NAME` is empty | Partially verified (no live send yet) | `services/compliance.py::email_blockers`; `test_missing_sender_identity_blocks` |
| A2 | Non-deceptive subject line | Templates are person-edited; `RE:`/`FW:` on non-replies blocked | Verified (rule); template wording unverified ⚖️ | `email_blockers` |
| A3 | Identify the message as an advertisement | Footer "This is a commercial message from …" (EN/ES) | Verified | `services/outreach.py::FOOTER` |
| A4 | Valid physical postal address | Footer includes `DF_BUSINESS_POSTAL_ADDRESS`; sending blocked if empty | Verified (presence); address validity is the operator's responsibility | `test_missing_sender_identity_blocks` |
| A5 | Clear opt-out mechanism | Unsubscribe link (one page, one button), RFC 8058 one-click header, reply "unsubscribe"/"baja" | Verified (mock), partially verified for real mail clients | `test_unsubscribe_link_and_one_click`, `test_opt_out_reply_*`, `test_spanish_opt_out` |
| A6 | Opt-out works ≥ 30 days after sending | Signed token with no expiry; breaks only if `DF_SECRET_KEY` changes | Verified (code); operational risk on key rotation | `security.py::unsign` (max_age None) |
| A7 | Honour opt-outs within 10 business days | Immediate: suppression + do-not-contact + cancel unsent messages | Verified | `test_opt_out_reply_suppresses_and_stops_everything` |
| A8 | No fee, no extra personal data, no more than a reply or single page to opt out | Unsubscribe page asks for nothing | Verified | `templates/unsubscribe.html` |
| A9 | Don't transfer or sell opted-out addresses | No export of suppressed addresses exists | Verified (absence of feature) | — |
| A10 | Responsibility for anyone sending on your behalf | Only the operator's mailbox is used; no third-party senders | Unverified (operational) | — |

## B. Email deliverability and provider rules [S-GBULK][S-GLIMITS][S-GMAIL][S-ESP]

| # | Requirement | How it's met | Status |
|---|---|---|---|
| B1 | SPF, DKIM (and DMARC) on the sending domain | Operator DNS setup; documented in integration setup | Unverified (operator) |
| B2 | One-click unsubscribe headers (List-Unsubscribe + List-Unsubscribe-Post) | Added to every outreach email | Verified (header generation tested) |
| B3 | Spam-complaint rate < 0.1% (never ≥ 0.3%) | Pilot stop conditions (pilot plan §7); Postmaster Tools monitoring is manual | Unverified (needs live sending) |
| B4 | Stay under Workspace limits (2,000/day; 500 on trial) | App cap 25/day by default | Verified (cap enforced) |
| B5 | Don't use ESPs that forbid cold email (SendGrid, Mailgun) | Not integrated; provider names are an allowlist | Verified |
| B6 | Gmail API restricted scope (`gmail.readonly`) | Use an **Internal** Workspace OAuth app, or SMTP/IMAP | Unverified (operator choice) |

## C. Telephone (human-dialled click-to-call only)

| # | Requirement | How it's met | Status | Source |
|---|---|---|---|---|
| C1 | No artificial/prerecorded/AI voice without consent | No such feature; Twilio TwiML only bridges two humans | Verified | [S-FCC-AI] |
| C2 | No autodialing | One call per click by a logged-in person | Verified | — |
| C3 | Federal DNC / TSR | Most B2B calls are exempt [S-TSR]; risk if a "business" number is an owner's personal cell [S-DNC]. Call published business lines only | Partially verified (operator practice) ⚖️ | [S-TSR][S-DNC] |
| C4 | Texas calling hours (9–9 Mon–Sat, noon–9 Sun) | Enforced on the Call button | Verified | [S-TX301] |
| C5 | **Texas ch. 302 registration ($10,000 security)** unless exempt | Calls positioned as meeting-booking only (§302.059(1)) | **Blocked for a repeated cold-calling program** ⚖️ (does a Zoom meeting count as "face-to-face"?) | [S-TX302] |
| C6 | Texas no-call list (ch. 304) | Applies to *consumer* goods/services; B2B appears outside it. Internal phone suppression is honoured anyway | Partially verified ⚖️ | [S-TX304] |
| C7 | Accurate caller ID | Twilio uses the operator's own or verified number only | Verified (config) | — |
| C8 | Call recording consent | Calls not recorded (`record="do-not-record"`) | Verified | [S-TXREC] |
| C9 | Texts / SMS (SB 140, eff. 2025-09-01) | No SMS feature | Verified (absent) | [S-TXSB140] |

## D. Data sources and privacy

| # | Requirement | How it's met | Status | Source |
|---|---|---|---|---|
| D1 | Use only sources you're allowed to store | TxDMV public list (CSV/XLS import), OSM with attribution, dealer websites per robots.txt; Google Places refused as a stored source | Verified | [S-TXDMV][S-OSM][S-GPT] |
| D2 | TxDMV list terms of use | No terms on the download page; copyright notice "© 2016 TxDMV". Public licensing record | Partially verified ⚖️ (confirm no restriction on commercial outreach use) | [S-TXDMV] |
| D3 | License-record emails | Free-mail addresses (63% in Fort Bend) are kept as unverified evidence and never used as the outreach address; business-domain emails are | Verified | `tests/test_txdmv.py` ⚖️ (whether using business-domain license emails is appropriate) |
| D4 | OSM attribution and ODbL | Attribution shown on records with OSM evidence | Verified | [S-OSM] |
| D5 | robots.txt (4xx allow; 5xx/unreachable disallow) | Implemented per RFC 9309; research off by default | Verified | [S-ROBOTS] |
| D6 | Overpass usage limits | One discovery run/day in the agent | Verified | [S-OVERPASS] |
| D7 | TDPSA | Business contacts acting commercially are outside "consumer"; SBA small businesses largely exempt; no data sales | Partially verified ⚖️ (confirm SBA small-business status) | [S-TDPSA] |
| D8 | Data minimisation and retention | Retention command redacts message bodies after 730 days and prunes logs; suppressions kept to honour opt-outs | Verified (command); schedule unverified (operator) | `tests/test_backup.py` |
| D9 | No invented facts | Templates use whitelisted verified fields only; observations require person-verified evidence; agent answers only from approved text | Verified | `test_adversarial.py`, `test_compliance.py` |

## E. Questions for an attorney ⚖️

1. **Chapter 302:** Does a call whose only purpose is booking a **Zoom** (not in-person) meeting fall under the §302.059(1) exemption? If not, register (with a $10,000 security) before regular cold calling, or keep calls to replies and inbound requests only.
2. **Business cell phones:** Is calling a number listed as a dealer's business phone acceptable if it turns out to be the owner's mobile (federal DNC, ch. 304)?
3. **TxDMV list:** Any restriction on using the public licensee list, and its business-domain emails, for B2B marketing?
4. **Email copy:** Review the EN/ES templates, subject lines, footer wording and approved answers.
5. **TDPSA:** Confirm the business is an SBA small business, and that no sensitive data is processed.
6. **Agent auto-sending:** Any additional disclosure needed when the software sends without a per-message human approval?
