"""ADF/XML (Auto-lead Data Format 1.0) lead parsing — the de-facto format used by listing sites
and dealer CRMs. Parsed with defusedxml to prevent XXE/entity-expansion attacks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from defusedxml import ElementTree as ET


@dataclass
class AdfLead:
    lead_id: str
    request_date: datetime | None
    name: str | None
    email: str | None
    phone: str | None
    comments: str
    vin: str | None
    stock: str | None
    year: str | None
    make: str | None
    model: str | None
    provider: str | None


def _text(el, path: str) -> str | None:
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else None


def parse_adf(xml: str) -> list[AdfLead]:
    root = ET.fromstring(xml)
    prospects = [root] if root.tag == "prospect" else root.findall("prospect")
    leads = []
    for p in prospects:
        names = {n.get("part", "full"): (n.text or "").strip() for n in p.findall("customer/contact/name")}
        name = names.get("full") or " ".join(x for x in (names.get("first"), names.get("last")) if x) or None
        rd = _text(p, "requestdate")
        try:
            req = datetime.fromisoformat(rd.replace("Z", "+00:00")).replace(tzinfo=None) if rd else None
        except ValueError:
            req = None
        lead_id = _text(p, "id") or f"{_text(p, 'provider/name') or 'adf'}:{rd}:{name}"
        leads.append(AdfLead(
            lead_id=lead_id, request_date=req, name=name, email=_text(p, "customer/contact/email"),
            phone=_text(p, "customer/contact/phone"),
            comments=_text(p, "customer/comments") or _text(p, "vehicle/comments") or "",
            vin=_text(p, "vehicle/vin"), stock=_text(p, "vehicle/stock"), year=_text(p, "vehicle/year"),
            make=_text(p, "vehicle/make"), model=_text(p, "vehicle/model"), provider=_text(p, "provider/name")))
    return leads
