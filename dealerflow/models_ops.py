"""Phase 2-4 models: dealership clients (tenancy), service operations, finance, SEO and reporting.

Conventions (in addition to those in models.py):
- ``Client`` is Phase 2's ``models_dealer.Client`` (extended with Phase 4 onboarding/contact columns).
  It is the tenant boundary: every client-owned table has an indexed, NOT NULL ``client_id``.
- Money is integer cents (``*_cents``) with an ISO currency. Phase 1's float ``CostEntry.amount``
  is the exception (usage-level cost ledger); see services/usage.py.
- ``source`` = manual | stripe | import | ... so manually entered records are always identifiable.
- An invoice or proposal is never cash: only ``Payment`` rows with status ``confirmed`` are collected.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

__all__ = [
    "IntegrationConnection", "SupportTicket", "OnboardingStep", "AutomationSetting", "Attachment", "WebhookEvent", "Alert", "Budget", "ServicePackage", "ClientAgreement", "Invoice", "InvoiceLine", "Payment", "CreditNote", "ExpenseCategory", "Expense", "BankTransaction", "SeoSite", "SeoAudit", "SeoPage", "SeoIssue", "SearchMetric", "ClientReport", "MONTHS_PER_PERIOD",
]

MONTHS_PER_PERIOD = {"monthly": 1, "quarterly": 3, "annual": 12}


class _Ts:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --- Tenancy and service operations -------------------------------------------------------


class IntegrationConnection(_Ts, Base):
    """Per-client channel connection. ``connected`` only after verification or documented authorization."""

    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("client_id", "kind", "provider", name="uq_integration_connection"),
        CheckConstraint("status in ('not_connected','sandbox','connected','error','disabled')", name="ck_integration_connections_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="not_connected", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    authorization_note: Mapped[str] = mapped_column(Text, default="")
    authorized_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Phase 3 customer messaging: pause one channel; provider account id (Meta page id, Twilio number, ADF token
    # hash …); non-secret config such as a sandbox failure mode for tests.
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(String(200), index=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default=text("'{}'"), nullable=False)


class SupportTicket(_Ts, Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        CheckConstraint("kind in ('client_issue','integration_failure','change_request','onboarding','other')", name="ck_support_tickets_kind"),
        CheckConstraint("priority in ('low','normal','high','urgent')", name="ck_support_tickets_priority"),
        CheckConstraint("status in ('open','in_progress','waiting_client','resolved','closed')", name="ck_support_tickets_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution: Mapped[str | None] = mapped_column(Text)


class OnboardingStep(_Ts, Base):
    __tablename__ = "onboarding_steps"
    __table_args__ = (
        UniqueConstraint("client_id", "code", name="uq_onboarding_step"),
        CheckConstraint("status in ('pending','in_progress','done','blocked','not_applicable')", name="ck_onboarding_steps_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class AutomationSetting(_Ts, Base):
    """Per-client automation switch. Enabling is gated by onboarding prerequisites."""

    __tablename__ = "automation_settings"
    __table_args__ = (UniqueConstraint("client_id", "feature", name="uq_automation_feature"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    feature: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime)


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachments_owner", "owner_type", "owner_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(300), nullable=False)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class WebhookEvent(Base):
    """Inbound webhook, stored once per (provider, event_id) so duplicates are ignored."""

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)  # received|processed|failed|ignored
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), default="warning", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Budget(Base):
    """Usage budget over ``cost_entries`` (which include estimates), with threshold and spike alerts."""

    __tablename__ = "budgets"
    __table_args__ = (CheckConstraint("period in ('daily','monthly')", name="ck_budgets_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str | None] = mapped_column(String(60))  # matches CostEntry.category; NULL = all
    period: Mapped[str] = mapped_column(String(10), default="monthly", nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_threshold_percent: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    spike_multiplier: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# --- Finance ------------------------------------------------------------------------------


class ServicePackage(_Ts, Base):
    """Catalog entry. Prices are editable data, never hardcoded; seeded rows are $0 *proposals*."""

    __tablename__ = "service_packages"
    __table_args__ = (
        CheckConstraint("setup_fee_cents >= 0 AND recurring_fee_cents >= 0", name="ck_service_packages_fees"),
        CheckConstraint("billing_frequency in ('monthly','quarterly','annual','one_time')", name="ck_service_packages_freq"),
        CheckConstraint("pricing_status in ('proposed','validated','retired')", name="ck_service_packages_pricing"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[list[Any]] = mapped_column(JSON, default=list)
    includes_packages: Mapped[list[Any]] = mapped_column(JSON, default=list)
    setup_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recurring_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billing_frequency: Mapped[str] = mapped_column(String(20), default="monthly", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pricing_status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)


class ClientAgreement(_Ts, Base):
    """Client-specific terms; prices are copied at signing so catalog edits never rewrite history."""

    __tablename__ = "client_agreements"
    __table_args__ = (
        CheckConstraint("status in ('pending','active','paused','cancelled','ended')", name="ck_client_agreements_status"),
        CheckConstraint("discount_percent BETWEEN 0 AND 100", name="ck_client_agreements_discount"),
        CheckConstraint("setup_fee_cents >= 0 AND recurring_fee_cents >= 0 AND discount_cents >= 0", name="ck_client_agreements_amounts"),
        CheckConstraint("billing_frequency in ('monthly','quarterly','annual','one_time')", name="ck_client_agreements_freq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    package_id: Mapped[int | None] = mapped_column(ForeignKey("service_packages.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    setup_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recurring_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billing_frequency: Mapped[str] = mapped_column(String(20), default="monthly", nullable=False)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # per billing period
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    features: Mapped[list[Any]] = mapped_column(JSON, default=list)
    custom_terms: Mapped[str] = mapped_column(Text, default="")
    cancelled_at: Mapped[date | None] = mapped_column(Date)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)

    package: Mapped[ServicePackage | None] = relationship()


class Invoice(_Ts, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("status in ('draft','issued','void','written_off')", name="ck_invoices_status"),
        CheckConstraint("due_date >= issue_date", name="ck_invoices_dates"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    agreement_id: Mapped[int | None] = mapped_column(ForeignKey("client_agreements.id", ondelete="SET NULL"))
    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    # draft (not revenue) | issued | void | written_off. Paid state is derived from payments.
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    void_reason: Mapped[str | None] = mapped_column(Text)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    lines: Mapped[list[InvoiceLine]] = relationship(back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.id")
    payments: Mapped[list[Payment]] = relationship(back_populates="invoice")
    credits: Mapped[list[CreditNote]] = relationship(back_populates="invoice")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        CheckConstraint("kind in ('setup','recurring','one_time','discount')", name="ck_invoice_lines_kind"),
        CheckConstraint("quantity > 0", name="ck_invoice_lines_qty"),
        CheckConstraint("(kind = 'discount' AND unit_amount_cents <= 0) OR (kind <> 'discount' AND unit_amount_cents >= 0)",
                        name="ck_invoice_lines_sign"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")

    @property
    def amount_cents(self) -> int:
        return self.quantity * self.unit_amount_cents


class Payment(_Ts, Base):
    """Money received. Only ``confirmed`` payments (with a received date) count as collected cash."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payments_amount"),
        CheckConstraint("status in ('pending','confirmed','failed','reversed')", name="ck_payments_status"),
        CheckConstraint("status <> 'confirmed' OR received_date IS NOT NULL", name="ck_payments_confirmed_date"),
        CheckConstraint("method in ('ach','wire','check','cash','card_via_processor','processor_payout','other')", name="ck_payments_method"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="confirmed", nullable=False)
    received_date: Mapped[date | None] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(30), default="other", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime)
    reconciled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str] = mapped_column(Text, default="")

    invoice: Mapped[Invoice | None] = relationship(back_populates="payments")


class CreditNote(_Ts, Base):
    """``refund`` = cash returned; ``credit`` = amount forgiven on an invoice."""

    __tablename__ = "credit_notes"
    __table_args__ = (
        CheckConstraint("kind in ('refund','credit')", name="ck_credit_notes_kind"),
        CheckConstraint("amount_cents > 0", name="ck_credit_notes_amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    issued_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True)

    invoice: Mapped[Invoice | None] = relationship(back_populates="credits")


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_acquisition: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # counts toward CAC
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Expense(_Ts, Base):
    """A recorded business expense (vendor bill, subscription charge). Bookkeeping, not usage estimates."""

    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_expenses_amount"),
        CheckConstraint("recurrence in ('one_time','monthly','quarterly','annual')", name="ck_expenses_recurrence"),
        CheckConstraint("reimbursement_status in ('not_applicable','pending','reimbursed','rejected')", name="ck_expenses_reimbursement"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    vendor: Mapped[str] = mapped_column(String(200), nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"), index=True, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    business_purpose: Mapped[str] = mapped_column(Text, nullable=False)
    recurrence: Mapped[str] = mapped_column(String(20), default="one_time", nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), index=True)
    project: Mapped[str | None] = mapped_column(String(200))
    reimbursement_status: Mapped[str] = mapped_column(String(20), default="not_applicable", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    category: Mapped[ExpenseCategory] = relationship()


class BankTransaction(Base):
    """Imported bank-statement line for reconciliation (+ inflow / - outflow)."""

    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    posted_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    import_batch: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="unmatched", nullable=False)  # unmatched|matched|ignored
    matched_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    matched_expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id", ondelete="SET NULL"))
    matched_credit_id: Mapped[int | None] = mapped_column(ForeignKey("credit_notes.id", ondelete="SET NULL"))


# --- SEO and client reporting -------------------------------------------------------------


class SeoSite(_Ts, Base):
    __tablename__ = "seo_sites"
    __table_args__ = (
        UniqueConstraint("client_id", "base_url", name="uq_seo_site"),
        CheckConstraint("max_pages BETWEEN 1 AND 500", name="ck_seo_sites_max_pages"),
        CheckConstraint("crawl_delay_seconds >= 0.5", name="ck_seo_sites_delay"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    authorization_note: Mapped[str] = mapped_column(Text, default="")
    authorized_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    max_pages: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    crawl_delay_seconds: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class SeoAudit(Base):
    __tablename__ = "seo_audits"
    __table_args__ = (CheckConstraint("status in ('queued','running','completed','failed')", name="ck_seo_audits_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    site_id: Mapped[int] = mapped_column(ForeignKey("seo_sites.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_skipped_robots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class SeoPage(Base):
    __tablename__ = "seo_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("seo_audits.id", ondelete="CASCADE"), index=True, nullable=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SeoIssue(_Ts, Base):
    """A finding, tracked across audits by ``fingerprint`` (code + URL) for before/after comparison."""

    __tablename__ = "seo_issues"
    __table_args__ = (
        UniqueConstraint("site_id", "fingerprint", name="uq_seo_issue_fingerprint"),
        CheckConstraint("severity in ('critical','high','medium','low','info')", name="ck_seo_issues_severity"),
        CheckConstraint("effort in ('small','medium','large')", name="ck_seo_issues_effort"),
        CheckConstraint("status in ('open','in_progress','resolved','wont_fix')", name="ck_seo_issues_status"),
        CheckConstraint("detection in ('measured','heuristic','ai')", name="ck_seo_issues_detection"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
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
    detection: Mapped[str] = mapped_column(String(12), nullable=False)
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)
    first_seen_audit_id: Mapped[int] = mapped_column(ForeignKey("seo_audits.id", ondelete="CASCADE"), nullable=False)
    last_seen_audit_id: Mapped[int] = mapped_column(ForeignKey("seo_audits.id", ondelete="CASCADE"), nullable=False)
    resolved_audit_id: Mapped[int | None] = mapped_column(ForeignKey("seo_audits.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class SearchMetric(Base):
    """Organic search data from an authorized source (Search Console export/API, analytics)."""

    __tablename__ = "search_metrics"
    __table_args__ = (UniqueConstraint("client_id", "metric_date", "source", "page", name="uq_search_metric"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    page: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    clicks: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    avg_position: Mapped[float | None] = mapped_column(Float)


class ClientReport(Base):
    """Monthly report. ``data`` is an immutable snapshot; clients see only ``final``/``sent`` reports."""

    __tablename__ = "client_reports"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_client_reports_period"),
        CheckConstraint("status in ('draft','final','sent')", name="ck_client_reports_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    generated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_to: Mapped[str | None] = mapped_column(String(255))
    delivery: Mapped[str | None] = mapped_column(String(40))
