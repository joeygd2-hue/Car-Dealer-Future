"""TxDMV Independent (GDN) Motor Vehicle Dealers List import profile.

Source: https://texasdmv.my.salesforce-sites.com/dealers/motorvehicledealerliststaging
("Download Independent(GDN) Motor Vehicle Dealers List"; Excel 97-2003, ~21,000 rows statewide).
Column names verified against the file published 2026-10-18 (see docs/audit/lead-sourcing.md).

Mapping rules (conservative: nothing is promoted to a verified fact):
- Keep LicenseType "Motor Vehicle", LicenseStatus starting "Active", AddressType "Physical".
- Name: DBAName if present (what customers see), else BusinessName. The legal name is evidence.
- DealerType "Franchise" -> franchise; blank -> independent_used, recorded as an unverified inference.
- BusinessEmail: used as the prospect's email only on a non-free-mail domain. Free-mail addresses
  (gmail, yahoo, ...) are kept as unverified evidence, never as the outreach address.
- Dedup key: license number (source "txdmv").
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from .normalize import domain_of, is_free_mail_domain, normalize_email
from .prospects import LeadRecord

SOURCE_URL = "https://texasdmv.my.salesforce-sites.com/dealers/motorvehicledealerliststaging"
REQUIRED = {"LicenseNumber", "BusinessName", "DBAName", "PhysicalAddress", "LicenseType", "DealerType"}


def is_txdmv(headers: Iterable[str]) -> bool:
    return REQUIRED <= {h.strip() for h in headers}


def to_records(rows: list[dict[str, str]], *, counties: list[str], cities: list[str],
               include_franchise: bool = False, data_date: str = "") -> tuple[list[tuple[int, LeadRecord]], Counter]:
    counties_l = {c.strip().lower() for c in counties if c.strip()}
    cities_l = {c.strip().lower() for c in cities if c.strip()}
    if not counties_l and not cities_l:
        raise ValueError("Choose at least one county or city; the statewide list has ~21,000 dealers")
    out: list[tuple[int, LeadRecord]] = []
    excluded: Counter = Counter()
    note = f"TxDMV Independent (GDN) dealer list{f' (data as of {data_date})' if data_date else ''}"
    for i, r in enumerate(rows, start=2):
        g = lambda k: (r.get(k) or "").strip()  # noqa: E731
        if counties_l and g("County").lower() not in counties_l and not (cities_l and g("City").lower() in cities_l):
            excluded["outside selected area"] += 1
            continue
        if not counties_l and g("City").lower() not in cities_l:
            excluded["outside selected area"] += 1
            continue
        if g("LicenseType") != "Motor Vehicle":
            excluded[f"license type: {g('LicenseType') or 'blank'}"] += 1
            continue
        if not g("LicenseStatus").startswith("Active"):
            excluded[f"license status: {g('LicenseStatus') or 'blank'}"] += 1
            continue
        if g("AddressType") and g("AddressType") != "Physical":
            excluded[f"address type: {g('AddressType')}"] += 1
            continue
        franchise = g("DealerType").lower() == "franchise"
        if franchise and not include_franchise:
            excluded["franchise dealer"] += 1
            continue
        signals = [("license:number", g("LicenseNumber"), note), ("license:status", g("LicenseStatus"), note),
                   ("license:expires", g("LicenseExpDate"), note), ("license:type", g("LicenseType"), note),
                   ("county", g("County"), note), ("legal_name", g("BusinessName"), note)]
        if g("ActiveDate"):
            signals.append(("license:active_since", g("ActiveDate"), note))
        if not franchise:
            signals.append(("dealership_type", "independent_used",
                            "Inferred: DealerType is blank on the TxDMV independent (GDN) list; not confirmed"))
        email = normalize_email(g("BusinessEmail"))
        prospect_email = None
        if email:
            if is_free_mail_domain(domain_of(email)):
                signals.append(("license_email", email, "Email on the dealer license record; personal-looking "
                                "free-mail address — not used for outreach unless a person verifies it"))
            else:
                prospect_email = email
        address = " ".join(x for x in (g("PhysicalAddress"), g("PhysAddressTwo")) if x) or None
        out.append((i, LeadRecord(
            name=g("DBAName") or g("BusinessName"), source="txdmv", external_id=g("LicenseNumber") or None,
            source_url=SOURCE_URL, address_line=address, city=g("City").title() or None, state=g("State") or None,
            postal_code=g("Zip")[:5] or None, phone=g("Phone") or None, email=prospect_email,
            dealership_type="franchise" if franchise else "independent_used", signals=signals,
            notes=note,
        )))
    return out, excluded
