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

from app.models.base import utcnow
from app.models.core import Client, IntegrationConnection, User
from app.models.dealer_ops import Appointment, Conversation, InventoryImport, Listing, PublishAttempt, Vehicle
from app.models.seo import ClientReport, SearchMetric, SeoIssue
from app.services import audit, costs

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

    imports = db.scalars(select(InventoryImport).where(InventoryImport.client_id == cid, InventoryImport.started_at >= s, InventoryImport.started_at < e)).all()
    convs = db.scalars(select(Conversation).where(Conversation.client_id == cid, Conversation.created_at >= s, Conversation.created_at < e)).all()
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

    usage = costs.usage_summary(db, start, end, client_id=cid)

    def metric(value, source: str, note: str | None = None) -> dict:
        return {"value": value, "source": source, **({"note": note} if note else {})}

    data = {
        "client": {"id": cid, "name": client.name},
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "channels": channels,
        "inventory": {
            "imports_run": metric(len(imports), "inventory_imports"),
            "vehicles_processed": metric(sum(i.rows_created + i.rows_updated for i in imports), "inventory_imports"),
            "vehicles_in_stock_at_end": metric(count(Vehicle, Vehicle.status == "available"), "vehicles"),
        },
        "listings": {
            "generated": metric(count(Listing, Listing.created_at >= s, Listing.created_at < e), "listings"),
            "approved": metric(count(Listing, Listing.approved_at >= s, Listing.approved_at < e), "listings"),
            "publishing_confirmed": metric(count(PublishAttempt, PublishAttempt.status == "published_confirmed", PublishAttempt.created_at >= s, PublishAttempt.created_at < e), "publish_attempts", "Confirmed by the channel's API"),
            "manual_publication_required": metric(count(PublishAttempt, PublishAttempt.status == "manual_required", PublishAttempt.created_at >= s, PublishAttempt.created_at < e), "publish_attempts", "Channel has no supported publishing API; the dealership must post these"),
        },
        "inquiries": {
            "received": metric(len(convs), "conversations", "Connected channels only"),
            "by_channel": by_channel,
            "median_first_response_minutes": metric(round(median(response_minutes), 1) if response_minutes else None, "conversations",
                                                    f"Based on {len(response_minutes)} conversations with both timestamps" if response_minutes else "No conversations with both timestamps"),
            "qualified_leads": metric(sum(1 for c in convs if c.lead_qualified), "conversations"),
            "escalations": metric(sum(1 for c in convs if c.escalated_at), "conversations"),
        },
        "appointments": {
            "test_drive_requests": metric(count(Appointment, Appointment.kind == "test_drive", Appointment.requested_at >= s, Appointment.requested_at < e), "appointments"),
            "confirmed": metric(count(Appointment, Appointment.status.in_(("confirmed", "completed")), Appointment.requested_at >= s, Appointment.requested_at < e), "appointments"),
        },
        "seo": {
            "issues_resolved": metric(count(SeoIssue, SeoIssue.resolved_at >= s, SeoIssue.resolved_at < e), "seo_issues", "Resolved = no longer detected by a re-audit, or marked resolved"),
            "issues_open_at_end": metric(count(SeoIssue, SeoIssue.status.in_(("open", "in_progress"))), "seo_issues"),
            "organic_search": search or {"value": None, "note": "No authorized Search Console or analytics data for this period"},
        },
        "service_usage": {
            "rows": usage["rows"],
            "estimated_cost_micros": usage["totals"]["estimated"],
            "verified_cost_micros": usage["totals"]["verified_billing"],
            "note": "Estimated costs are computed from usage x published rates and are not invoices.",
        },
        "attribution_note": ATTRIBUTION_NOTE,
    }
    return data


def generate_report(db: Session, client: Client, start: date, end: date, actor: User | None) -> ClientReport:
    if end < start:
        raise ValueError("End before start")
    r = ClientReport(client_id=client.id, period_start=start, period_end=end, data=build_report_data(db, client, start, end),
                     generated_by_user_id=actor.id if actor else None, status="draft")
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


def send_report(db: Session, report: ClientReport, to: str, actor: User) -> str:
    """Send via the configured, authorized email provider. Sandbox when none is authorized."""
    from app.integrations.email import send_email
    from app.services.onboarding import automation_enabled

    conn = db.scalar(select(IntegrationConnection).where(IntegrationConnection.client_id == report.client_id, IntegrationConnection.kind == "report_email", IntegrationConnection.status == "connected"))
    allow_live = conn is not None
    email = send_email(
        db, purpose="client_report", to=to, subject=f"Monthly report {report.period_start:%B %Y}",
        body_text=report_text(report), client_id=report.client_id, allow_live=allow_live,
        idempotency_key=f"report:{report.id}:{to}",
    )
    if email.status == "sent":
        report.status = "sent"
    elif report.status == "draft":
        report.status = "final"
    report.sent_at, report.sent_to = utcnow(), to
    audit.record(db, "report.sent", actor=actor, entity=report, details={"to_domain": to.split("@")[-1], "delivery": email.status, "auto": automation_enabled(db, report.client_id, "monthly_report_email")})
    return email.status
