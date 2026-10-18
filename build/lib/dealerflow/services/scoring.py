"""Explainable lead scoring.

Each factor scores 0..1 from stored facts only, is multiplied by its configurable weight, and
reports (a) the reason, (b) the evidence it used and (c) what is still unverified. Nothing here
estimates revenue, sales volume or demographics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import Evidence, InboundMessage, OutreachMessage, Prospect
from .prospects import miles_between
from .settings_store import get_setting


@dataclass
class Factor:
    key: str
    label: str
    weight: float
    value: float  # 0..1
    reason: str
    evidence_ids: list[int] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    @property
    def points(self) -> float:
        return round(self.weight * self.value, 1)


def _signals(p: Prospect) -> dict[str, list[Evidence]]:
    out: dict[str, list[Evidence]] = {}
    for ev in p.evidence:
        out.setdefault(ev.field, []).append(ev)
    return out


def _ids(evs: list[Evidence]) -> list[int]:
    return [e.id for e in evs if e.id]


def score_prospect(db: Session, p: Prospect) -> dict[str, Any]:
    s = get_settings()
    weights = get_setting(db, "scoring_weights")
    sig = _signals(p)
    factors: list[Factor] = []

    # 1. Target market fit
    t = p.dealership_type
    fit = {"independent_used": 1.0, "buy_here_pay_here": 1.0, "wholesale": 0.3, "other": 0.2,
           "franchise": 0.0, "unknown": 0.4}[t if t in ("independent_used", "buy_here_pay_here", "wholesale", "other",
                                                        "franchise") else "unknown"]
    type_ev = sig.get("dealership_type", [])
    unv = [] if any(e.verified for e in type_ev) else ["dealership type not verified by a person"]
    factors.append(Factor("target_market_fit", "Target market fit", weights["target_market_fit"], fit,
                          f"Dealership type recorded as '{t}'." + (" Unknown types get partial credit." if t == "unknown" else ""),
                          _ids(type_ev), unv))

    # 2. Geographic fit
    if p.lat is not None and p.lng is not None:
        d = miles_between(s.target_lat, s.target_lng, p.lat, p.lng)
        val = 1.0 if d <= s.target_radius_miles * 0.5 else (0.6 if d <= s.target_radius_miles else 0.0)
        reason = f"{d:.1f} miles from the target centre (radius {s.target_radius_miles:.0f} mi)."
        unv = []
    elif p.city:
        inside = p.city.strip().lower() in {c.lower() for c in s.target_city_list}
        val = 0.8 if inside else 0.1
        reason = f"City '{p.city}' is {'in' if inside else 'not in'} the target city list (no coordinates)."
        unv = ["coordinates missing; distance not measured"]
    else:
        val, reason, unv = 0.0, "No location recorded.", ["location unknown"]
    factors.append(Factor("geographic_fit", "Geographic fit", weights["geographic_fit"], val, reason,
                          _ids(sig.get("city", []) + sig.get("lat", [])), unv))

    # 3. Publicly observable inventory / listing activity
    inv_ev = sig.get("signal:inventory_count", [])
    if inv_ev:
        latest = max(inv_ev, key=lambda e: e.collected_at)
        try:
            n = int(float(latest.value))
        except ValueError:
            n = 0
        val = 1.0 if n >= 30 else (0.7 if n >= 10 else 0.4)
        reason = f"About {n} vehicles observed on {latest.source_url or 'the listing page'} on {latest.collected_at:%Y-%m-%d}."
        unv = [] if latest.verified else ["inventory count is an automated observation, not confirmed"]
        ids = [latest.id]
    elif p.inventory_url:
        val, reason, unv, ids = 0.5, "Inventory page URL known; volume not observed.", ["inventory volume unknown"], _ids(sig.get("inventory_url", []))
    else:
        val, reason, unv, ids = 0.0, "No inventory page recorded.", ["inventory activity unknown"], []
    factors.append(Factor("inventory_activity", "Inventory / listing activity", weights["inventory_activity"], val, reason, ids, unv))

    # 4. Website and listing quality indicators (opportunity, not a judgement of the business)
    checks = {k: sig.get(f"signal:{k}") for k in ("https", "mobile_viewport", "contact_form", "chat_widget", "online_credit_app")}
    if not p.website:
        val, reason, unv = 0.5, "No website on record; may need web presence (unverified).", ["website existence not confirmed"]
        ids: list[int] = []
    elif not any(checks.values()):
        val, reason, unv, ids = 0.3, "Website known but not yet researched.", ["website not analysed"], []
    else:
        present = [k for k, v in checks.items() if v and v[-1].value.lower() == "true"]
        absent = [k for k, v in checks.items() if v and v[-1].value.lower() == "false"]
        # More gaps in lead-capture features = more room for our service.
        val = min(1.0, 0.3 + 0.15 * len(absent))
        reason = f"Observed present: {', '.join(present) or 'none'}; observed absent: {', '.join(absent) or 'none'}."
        unv = ["automated website observations; confirm before mentioning to the dealer"]
        ids = [v[-1].id for v in checks.values() if v]
    factors.append(Factor("web_presence", "Website / listing indicators", weights["web_presence"], val, reason, ids, unv))

    # 5. Contact available
    verified_contacts = [c for c in p.contacts if c.verified and (c.email or c.phone)]
    if verified_contacts:
        c = verified_contacts[0]
        val, reason, unv = 1.0, f"Verified contact: {c.name or 'unnamed'} ({c.role or 'role unknown'}).", []
    elif p.email:
        val, reason, unv = 0.6, "Public business email on record (no named contact).", ["no verified named contact"]
    elif p.phone_e164:
        val, reason, unv = 0.3, "Only a public business phone on record.", ["no email or named contact"]
    else:
        val, reason, unv = 0.0, "No contact route on record.", ["no contact details"]
    factors.append(Factor("contact_available", "Relevant contact available", weights["contact_available"], val, reason,
                          _ids(sig.get("email", []) + sig.get("phone_e164", [])), unv))

    # 6. Bilingual lead response: only from observed signals (Spanish site content, "se habla español")
    es = sig.get("signal:spanish_content", [])
    if es and es[-1].value.lower() == "true":
        val, reason = 1.0, "Spanish-language content observed on the dealer's own materials."
        unv = [] if es[-1].verified else ["observation not confirmed by a person"]
    elif es:
        val, reason, unv = 0.3, "No Spanish content observed on the website.", ["absence of Spanish content says little about customers"]
    else:
        val, reason, unv = 0.3, "Not researched.", ["bilingual need unknown"]
    factors.append(Factor("bilingual_opportunity", "Bilingual lead response potential", weights["bilingual_opportunity"],
                          val, reason, _ids(es), unv))

    # 7. Engagement history
    sent = db.scalar(select(func.count()).select_from(OutreachMessage).where(
        OutreachMessage.prospect_id == p.id, OutreachMessage.sent_at.is_not(None))) or 0
    replies = list(db.scalars(select(InboundMessage.classification).where(InboundMessage.prospect_id == p.id)))
    positive = sum(1 for r in replies if r in ("interested", "meeting_request", "question"))
    if positive:
        val, reason = 1.0, f"{positive} positive repl{'y' if positive == 1 else 'ies'}."
    elif replies:
        val, reason = 0.4, f"{len(replies)} repl{'y' if len(replies) == 1 else 'ies'}, none positive."
    elif sent:
        val, reason = 0.1, f"{sent} message(s) sent, no reply yet."
    else:
        val, reason = 0.0, "No outreach yet."
    factors.append(Factor("engagement", "Engagement and reply history", weights["engagement"], val, reason))

    total_w = sum(f.weight for f in factors) or 1
    total = round(100 * sum(f.weight * f.value for f in factors) / total_w, 1)
    eligible = not p.do_not_contact
    breakdown = {
        "total": total if eligible else 0.0,
        "eligible": eligible,
        "ineligible_reason": None if eligible else "Do not contact",
        "threshold": get_setting(db, "qualification_threshold"),
        "factors": [dict(asdict(f), points=f.points) for f in factors],
        "unverified": sorted({u for f in factors for u in f.unverified}),
        "computed_at": utcnow().isoformat(),
    }
    p.score, p.score_breakdown, p.scored_at = breakdown["total"], breakdown, utcnow()
    return breakdown


def score_many(db: Session, prospects: list[Prospect]) -> int:
    for p in prospects:
        score_prospect(db, p)
    return len(prospects)
