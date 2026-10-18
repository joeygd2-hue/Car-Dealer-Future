# Lead sourcing audit: TxDMV dealer list (2026-10-18)

## The actual public file

| Item | Finding |
|---|---|
| Page | https://texasdmv.my.salesforce-sites.com/dealers → *Motor Vehicle Dealers List* → "Independent (GDN) Motor Vehicle Dealers List" (`/dealers/motorvehicledealerliststaging`) |
| Page text | "A complete spreadsheet of active Independent(GDN) Motor Vehicle Dealers is available for download … Data is current as of 10/18/2026." |
| Download | `servlet.FileDownload?…file=00Pcs000023KBPaEAO`, `application/vnd.ms-excel`, `Motor Vehicle Dealer-2026-Oct-18-05-30-02.xls`, 9.6 MB, Excel 97-2003 |
| Terms | No usage terms on the page or file; "© Copyright 2016, Texas Department of Motor Vehicles"; links to a privacy policy and to open-records requests. ⚖️ Ask counsel to confirm commercial B2B outreach use (compliance checklist D2) |
| How obtained | Downloaded once by an automated headless browser (Playwright) acting as a normal visitor: one page view and one file download. No robots.txt-disallowed paths, logins or CAPTCHAs. The file is **not** committed to the repository |
| Rows / columns | 20,957 dealers statewide; 23 columns: County, LicenseNumber, LicenseStatus, LicenseExpDate, BusinessName, DBAName, AddressType, PhysicalAddress, PhysAddressTwo, City, State, Zip, MailingAddress, MailAddressTwo, MailingCity, MailingState, MailingZip, Phone, BusinessEmail, LicenseType, ActiveDate, DealerType, BondCompany |
| Quirks | Despite the "Independent" title, 5,064 rows are `DealerType = Franchise`. The `AddressType` header contains tab characters. Zip is ZIP+4. State is "Texas". 28% of licenses are not motor-vehicle (trailers, motorcycles, wholesale) |

## Problems found and fixed

| Problem with the Phase 1 importer | Fix | Test |
|---|---|---|
| Could not read `.xls` | `xlrd` support | `test_txdmv_import_through_the_web_form` uses the real column layout |
| 20,957 rows exceeded the 5,000-row limit | TxDMV profile requires a county/city filter, applied before intake | `test_txdmv_profile_filters_maps_and_keeps_provenance` |
| CamelCase headers didn't match any alias | Dedicated profile, detected automatically from the columns | same |
| Franchise, trailer, motorcycle, wholesale, expired and secondary-address rows would have been imported | Excluded and reported by reason; franchise optional | same |
| Free-mail license emails (63% in Fort Bend) would have become outreach addresses | Kept as **unverified** `license_email` evidence; only business-domain emails populate the prospect email | same |
| Two different licensees sharing a phone number were merged | TxDMV license numbers are treated as distinct businesses | same; real-file rerun: 0 false merges |
| Legal names (e.g. "Htown Automotive Group LLC") would be used in emails | DBA is used as the name; legal name kept as evidence | same |

## Dry-run results on the real file (not saved)

| Filter | Imported | Matched | Excluded (besides other areas) |
|---|---|---|---|
| County = Fort Bend | 191 | 0 | 34 franchise, 21 motorcycle, 23 wholesale, 9 travel trailer, 33 utility trailer, 3 expired, 1 auction, 4 additional/supplemental addresses |
| Cities = Sugar Land, Stafford, Missouri City, Richmond, Rosenberg | 160 | 0 | 21 franchise, 64 other license types, 2 expired, 2 secondary addresses |

Fort Bend (imported into a scratch database only): 191 dealers, 190 with a phone, all ZIPs valid, 33 business-domain emails,
157 free-mail emails kept as evidence only. Re-importing the same file created 0 duplicates.

**Scoring on real data:** with default weights, scores range 39.5–58.0 and only **33 of 191** reach the 55 threshold. They are
exactly the 33 with a business-domain email: the license list has no website, coordinates or inventory, so those factors stay
unscored until researched. For the pilot, about 33 Fort Bend dealers can be emailed directly. The rest need a person to verify a
contact (website or phone) first. That is deliberate (no guessed contacts), but it is the main constraint on pilot volume.

## Provenance and certainty

Every imported field has an `Evidence` row with `source_type = txdmv`, the list URL, the collection time, and a note
"TxDMV Independent (GDN) dealer list (data as of …)". Inferences are marked unverified:

- `dealership_type = independent_used` is **inferred** from a blank DealerType. The score shows "dealership type not verified by a person".
- `license_email` is **unverified** and never used for outreach until a person verifies it.
- License number, status, expiry and county are recorded as reported by TxDMV.

## Respecting sources

| Source | Rule | Enforcement |
|---|---|---|
| TxDMV | Manual download; no scraping of search results | The app never contacts TxDMV; import is file-based |
| OpenStreetMap | ODbL attribution; at most ~100 Overpass queries/day | Attribution on prospect pages; agent runs discovery at most once a day |
| Google Places | Only place IDs may be stored | Refused as a stored source |
| Dealer websites | robots.txt; at most 2 pages; public IPs only; identifying user agent | `integrations/website.py`; off by default |
| Retention | Message bodies redacted after 730 days; suppressions kept | `python -m dealerflow retention` |

## Contact restrictions before every action

Checked at draft, at approval **and immediately before the provider call** (`send_email_job` → `email_blockers` with the
actual provider): suppression (email, @domain, phone), do-not-contact, simulated or reserved addresses, stop conditions,
touch cap, cooldown, duplicates, daily cap, campaign pause, agent state and operating hours (for agent-approved
messages), emergency stop and disabled providers. Calls check do-not-contact, phone suppression and Texas calling hours
at click time. Tests: `tests/test_compliance.py`, `tests/test_adversarial.py`.
