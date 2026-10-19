"""Monthly client reports (Phase 4, Step 4).

Only metrics backed by recorded data are included. Each metric carries its
source; channels that are not connected are listed explicitly; no statement
attributes sales or revenue to our service.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import utcnow
from ..integrations.listing_channels import CHANNELS as LISTING_CHANNELS
from ..models import (
    Appointment,
    Client,
    ClientReport,
    Conversation,
    IntegrationConnection,
    SearchMetric,
    SeoIssue,
    User,
)
from ..models_dealer import (
    ChannelConnection,
    ChannelPublication,
    ImportRun,
    InventorySource,
    ListingReview,
    ListingRevision,
    Vehicle,
)
from . import client_email, usage
from .inventory.feeds import FEED_TYPES

CONNECTABLE_CHANNELS = {
    "inventory_feed": "Inventory feed",
    "customer_messaging": "Customer messaging (email/SMS/web chat)",
    "facebook_marketplace": "Facebook Marketplace",
    "calendar": "Appointment calendar",
    "search_console": "Google Search Console",
    "website_analytics": "Website analytics",
    "report_email": "Report email delivery",
}

ATTRIBUTION_NOTE = (
    "This report summarizes activity recorded by our platform. It does not measure, and makes no claim about, "
    "additional vehicle sales or revenue caused by our services."
)


def _range(start: date, end: date) -> tuple[datetime, datetime]:
    return datetime(start.year, start.month, start.day), datetime(end.year, end.month, end.day) + timedelta(days=1)


def build_report_data(db: Session, client: Client, start: date, end: date) -> dict:
    s, e = _range(start, end)
    cid = client.id

    def count(model, *conds) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(model.client_id == cid, *conds)) or 0)

    conns = {c.kind: c for c in db.scalars(select(IntegrationConnection).where(IntegrationConnection.client_id == cid))}
    channels = []
    for kind, label in CONNECTABLE_CHANNELS.items():
        c = conns.get(kind)
        channels.append({"kind": kind, "label": label, "status": c.status if c else "not_connected", "provider": c.provider if c else None,
                         "verified_at": c.verified_at.isoformat() if c and c.verified_at else None})

    # Phase 2 data. Plain session: every query filters client_id explicitly.
    for src in db.scalars(select(InventorySource).where(InventorySource.client_id == cid, InventorySource.source_type.in_(tuple(FEED_TYPES)))):
        st = ("disabled" if not src.enabled else "connected" if src.last_status in ("succeeded", "partial")
              else "error" if src.last_status == "failed" else "not_connected")
        channels.append({"kind": f"inventory_source:{src.id}", "label": f"Inventory source: {src.name}", "status": st,
                         "provider": src.source_type, "verified_at": src.last_sync_at.isoformat() if src.last_sync_at else None})
    for cc in db.scalars(select(ChannelConnection).where(ChannelConnection.client_id == cid)):
        adapter = LISTING_CHANNELS.get(cc.channel)
        st = "sandbox" if adapter and adapter.simulated else "connected" if cc.live_publishing and cc.authorized_at else "not_connected"
        channels.append({"kind": f"listing_channel:{cc.channel}", "label": f"Listing channel: {cc.channel}", "status": st,
                         "provider": adapter.mode if adapter else None, "verified_at": cc.authorized_at.isoformat() if cc.authorized_at else None})
    imports = db.scalars(select(ImportRun).where(ImportRun.client_id == cid, ImportRun.started_at >= s, ImportRun.started_at < e)).all()
    pubs = db.execute(select(ChannelPublication.channel, ChannelPublication.status, ChannelPublication.confirmation_type, func.count())
                      .where(ChannelPublication.client_id == cid, ChannelPublication.confirmed_at >= s, ChannelPublication.confirmed_at < e,
                             ChannelPublication.status == "published_confirmed")
                      .group_by(ChannelPublication.channel, ChannelPublication.status, ChannelPublication.confirmation_type)).all()
    simulated_channels = {k for k, a in LISTING_CHANNELS.items() if a.simulated}
    api_confirmed = sum(n for ch, _st, ct, n in pubs if ct == "api" and ch not in simulated_channels)
    manual_confirmed = sum(n for ch, _st, ct, n in pubs if ct == "manual_attested" and ch not in simulated_channels)
    simulated_pubs = sum(n for ch, _st, _ct, n in pubs if ch in simulated_channels)

    def distinct_listings(q) -> int:
        return int(db.scalar(select(func.count()).select_from(q.distinct().subquery())) or 0)
    all_convs = db.scalars(select(Conversation).where(Conversation.client_id == cid, Conversation.created_at >= s, Conversation.created_at < e)).all()
    convs = [c for c in all_convs if not c.simulated]  # SIMULATED test conversations are reported separately, never as real
    response_minutes = [
        (c.first_response_at - c.first_inbound_at).total_seconds() / 60
        for c in convs if c.first_inbound_at and c.first_response_at and c.first_response_at >= c.first_inbound_at
    ]
    by_channel: dict[str, int] = {}
    for c in convs:
        by_channel[c.channel] = by_channel.get(c.channel, 0) + 1

    search = None
    sm = db.execute(
        select(SearchMetric.source, func.sum(SearchMetric.clicks), func.sum(SearchMetric.impressions), func.count())
        .where(SearchMetric.client_id == cid, SearchMetric.metric_date.between(start, end))
        .group_by(SearchMetric.source)
    ).all()
    if sm:
        search = [{"source": src, "clicks": int(cl or 0), "impressions": int(im or 0), "days_with_data": n} for src, cl, im, n in sm]

    use = usage.summary(db, start, end, client_id=cid)

    def metric(value, source: str, note: str | None = None) -> dict:
        return {"value": value, "source": source, **({"note": note} if note else {})}

    data = {
        "client": {"id": cid, "name": client.name},
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "channels": channels,
        "inventory": {
            "imports_run": metric(len(imports), "import_runs"),
            "imports_failed": metric(sum(1 for i in imports if i.status == "failed"), "import_runs"),
            "vehicles_processed": metric(sum(int((i.counts or {}).get("created", 0)) + int((i.counts or {}).get("updated", 0)) for i in imports), "import_runs"),
            "vehicles_added": metric(count(Vehicle, Vehicle.first_seen_at >= s, Vehicle.first_seen_at < e), "vehicles"),
            "vehicles_available_now": metric(count(Vehicle, Vehicle.inventory_status.in_(("available", "pending"))), "vehicles",
                                             "Current count when the report was generated, not a period figure"),
        },
        "listings": {
            "generated": metric(distinct_listings(select(ListingRevision.listing_id).where(
                ListingRevision.client_id == cid, ListingRevision.origin != "human_edit", ListingRevision.created_at >= s, ListingRevision.created_at < e)),
                "listing_revisions", "Listings with a generated draft in the period"),
            "approved": metric(distinct_listings(select(ListingReview.listing_id).where(
                ListingReview.client_id == cid, ListingReview.decision == "approved", ListingReview.created_at >= s, ListingReview.created_at < e)),
                "listing_reviews"),
            "publishing_confirmed_via_api": metric(api_confirmed, "channel_publications", "Confirmed by the channel's API"),
            "publishing_confirmed_manually": metric(manual_confirmed, "channel_publications",
                                                    "A person recorded the live URL; the platform cannot verify it is still live"),
            "manual_publication_required": metric(count(ChannelPublication, ChannelPublication.status == "manual_required"), "channel_publications",
                                                  "Current count; channel has no supported publishing API, the dealership must post these"),
            "simulated_excluded": simulated_pubs,
        },
        "inquiries": {
            "received": metric(len(convs), "conversations", "Website chat, ADF leads and logged Marketplace messages; excludes SIMULATED test conversations"),
            "simulated_excluded": len(all_convs) - len(convs),
            "by_channel": by_channel,
            "median_first_response_minutes": metric(round(median(response_minutes), 1) if response_minutes else None, "conversations",
                                                    (f"Based on {len(response_minutes)} conversations; only replies that reached the customer count"
                                                     if response_minutes else "No conversation had a reply that reached the customer")),
            "qualified_leads": metric(sum(1 for c in convs if c.lead_qualified), "conversations"),
            "escalations": metric(sum(1 for c in convs if c.escalation_status != "none"), "conversations"),
            "opted_out": metric(sum(1 for c in convs if c.opted_out), "conversations"),
        },
        "appointments": {
            "test_drive_requests": metric(count(Appointment, Appointment.kind == "test_drive", Appointment.requested_at >= s, Appointment.requested_at < e), "appointments",
                                          "Requests from customers; a request is not a booking"),
            "confirmed": metric(count(Appointment, Appointment.status.in_(("confirmed", "completed")), Appointment.requested_at >= s, Appointment.requested_at < e), "appointments"),
        },
        "seo": {
            "issues_resolved": metric(count(SeoIssue, SeoIssue.resolved_at >= s, SeoIssue.resolved_at < e), "seo_issues", "Resolved = no longer detected by a re-audit, or marked resolved"),
            "issues_open_at_end": metric(count(SeoIssue, SeoIssue.status.in_(("open", "in_progress"))), "seo_issues"),
            "organic_search": search or {"value": None, "note": "No authorized Search Console or analytics data for this period"},
        },
        "service_usage": {
            "rows": use["rows"],
            "estimated_cost_usd": use["totals"]["estimated"],
            "verified_cost_usd": use["totals"]["verified_billing"],
            "note": "Estimated costs are computed from measured usage x published rates and are not invoices.",
        },
        "attribution_note": ATTRIBUTION_NOTE,
    }
    return data


def generate_report(db: Session, client: Client, start: date, end: date, actor: User | None) -> ClientReport:
    if end < start:
        raise ValueError("End before start")
    r = ClientReport(client_id=client.id, period_start=start, period_end=end, data=build_report_data(db, client, start, end),
                     generated_by_id=actor.id if actor else None, status="draft")
    db.add(r)
    db.flush()
    audit.record(db, "report.generated", actor=actor, entity=r)
    return r


def previous_month(today: date) -> tuple[date, date]:
    first_this = date(today.year, today.month, 1)
    last_prev = first_this - timedelta(days=1)
    return date(last_prev.year, last_prev.month, 1), last_prev


def report_text(report: ClientReport) -> str:
    d = report.data
    lines = [f"Monthly report — {d['client']['name']}", f"Period: {d['period']['start']} to {d['period']['end']}", ""]
    for section in ("inventory", "listings", "inquiries", "appointments"):
        lines.append(section.replace("_", " ").title())
        for k, v in d[section].items():
            if isinstance(v, dict) and "value" in v:
                lines.append(f"  - {k.replace('_', ' ')}: {v['value'] if v['value'] is not None else 'not available'}")
        lines.append("")
    connected = [c["label"] for c in d["channels"] if c["status"] == "connected"]
    not_connected = [c["label"] for c in d["channels"] if c["status"] != "connected"]
    lines.append("Connected channels: " + (", ".join(connected) or "none"))
    lines.append("Not connected: " + (", ".join(not_connected) or "none"))
    lines += ["", d["attribution_note"]]
    return "\n".join(lines)


def send_report(db: Session, report: ClientReport, to: str, actor: User | None) -> str:
    """Email the report through the authorized provider; otherwise it is recorded as SIMULATED / not sent."""
    if report.delivery == "sent" and report.sent_to == to:
        return "sent"  # never send the same report twice to the same recipient
    client = db.get(Client, report.client_id)
    outcome = client_email.send(db, client, to=to, subject=f"Monthly report {report.period_start:%B %Y}",
                                body=report_text(report), key=f"report:{report.id}:{to}")
    if report.status == "draft":
        report.status = "final"
    if outcome == "sent":
        report.status = "sent"
    report.sent_at, report.sent_to, report.delivery = utcnow(), to, outcome[:40]
    audit.record(db, "report.sent", actor=actor, entity=report, details={"to_domain": to.split("@")[-1], "delivery": outcome})
    return outcome
