"""Phase 4: SEO audit service and client reports."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, utcnow


class SeoSite(IdMixin, TimestampMixin, Base):
    __tablename__ = "seo_sites"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(300), nullable=False)
    # Audits only run when the dealership has authorized us to audit this site.
    authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    authorization_note: Mapped[str | None] = mapped_column(Text)
    authorized_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    max_pages: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    crawl_delay_seconds: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    __table_args__ = (
        UniqueConstraint("client_id", "base_url", name="uq_seo_site"),
        CheckConstraint("max_pages BETWEEN 1 AND 500", name="ck_seo_max_pages"),
        CheckConstraint("crawl_delay_seconds >= 0.5", name="ck_seo_crawl_delay"),
    )


class SeoAudit(IdMixin, Base):
    __tablename__ = "seo_audits"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    site_id: Mapped[int] = mapped_column(ForeignKey("seo_sites.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_skipped_robots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (
        CheckConstraint("status in ('queued','running','completed','failed')", name="ck_seo_audit_status"),
    )


class SeoPage(IdMixin, Base):
    __tablename__ = "seo_pages"
    audit_id: Mapped[int] = mapped_column(ForeignKey("seo_audits.id", ondelete="CASCADE"), index=True, nullable=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class SeoIssue(IdMixin, TimestampMixin, Base):
    """A finding. `fingerprint` lets us track the same issue across audits (before/after)."""

    __tablename__ = "seo_issues"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    site_id: Mapped[int] = mapped_column(ForeignKey("seo_sites.id", ondelete="CASCADE"), index=True, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    impact: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_fix: Mapped[str] = mapped_column(Text, nullable=False)
    effort: Mapped[str] = mapped_column(String(10), nullable=False)
    responsible_party: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    # measured = directly observed; heuristic = rule-based opportunity; ai = AI-generated recommendation
    detection: Mapped[str] = mapped_column(String(12), nullable=False)
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)
    first_seen_audit_id: Mapped[int] = mapped_column(ForeignKey("seo_audits.id", ondelete="CASCADE"), nullable=False)
    last_seen_audit_id: Mapped[int] = mapped_column(ForeignKey("seo_audits.id", ondelete="CASCADE"), nullable=False)
    resolved_audit_id: Mapped[int | None] = mapped_column(ForeignKey("seo_audits.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        UniqueConstraint("site_id", "fingerprint", name="uq_seo_issue_fingerprint"),
        CheckConstraint("severity in ('critical','high','medium','low','info')", name="ck_seo_severity"),
        CheckConstraint("effort in ('small','medium','large')", name="ck_seo_effort"),
        CheckConstraint("status in ('open','in_progress','resolved','wont_fix')", name="ck_seo_issue_status"),
        CheckConstraint("detection in ('measured','heuristic','ai')", name="ck_seo_detection"),
    )


class SearchMetric(IdMixin, Base):
    """Organic search metrics from an authorized source (Search Console export/API, analytics)."""

    __tablename__ = "search_metrics"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    clicks: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    avg_position: Mapped[float | None] = mapped_column(Float)
    page: Mapped[str | None] = mapped_column(String(1000))
    __table_args__ = (UniqueConstraint("client_id", "metric_date", "source", "page", name="uq_search_metric"),)


class ClientReport(IdMixin, Base):
    """A monthly client report. `data` is an immutable snapshot of the metrics at generation time."""

    __tablename__ = "client_reports"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    generated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)  # draft|final|sent
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_to: Mapped[str | None] = mapped_column(String(254))
    __table_args__ = (CheckConstraint("period_end >= period_start", name="ck_report_period"),)
