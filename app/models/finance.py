"""Phase 4 finance models: service catalog, agreements, invoices, payments, expenses.

Rules enforced here and in services/finance.py:
* An invoice (or proposal) is never cash. Only `Payment` rows with status
  `confirmed` count as collected.
* No raw payment-card data is stored. Free-text fields are screened for PANs.
* Every record carries `source` (manual | stripe | import | ...) so manually
  entered data is clearly identified.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, TimestampMixin

BILLING_FREQUENCIES = ("monthly", "quarterly", "annual", "one_time")
MONTHS_PER_PERIOD = {"monthly": 1, "quarterly": 3, "annual": 12}


class ServicePackage(IdMixin, TimestampMixin, Base):
    __tablename__ = "service_packages"
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    features: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    includes_packages: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    setup_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recurring_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billing_frequency: Mapped[str] = mapped_column(String(20), default="monthly", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Proposed prices have not been validated with the market.
    pricing_status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    __table_args__ = (
        CheckConstraint("setup_fee_cents >= 0 AND recurring_fee_cents >= 0", name="ck_package_fees"),
        CheckConstraint("billing_frequency in ('monthly','quarterly','annual','one_time')", name="ck_package_freq"),
        CheckConstraint("pricing_status in ('proposed','validated','retired')", name="ck_package_pricing_status"),
    )


class ClientAgreement(IdMixin, TimestampMixin, Base):
    """Client-specific terms. Copies prices at signing so catalog edits don't rewrite history."""

    __tablename__ = "client_agreements"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    package_id: Mapped[int | None] = mapped_column(ForeignKey("service_packages.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    setup_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recurring_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billing_frequency: Mapped[str] = mapped_column(String(20), default="monthly", nullable=False)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # whole percent 0-100
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # per billing period
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    features: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    custom_terms: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[date | None] = mapped_column(Date)
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    signed_by_client: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    package: Mapped[ServicePackage | None] = relationship()

    __table_args__ = (
        CheckConstraint("status in ('pending','active','paused','cancelled','ended')", name="ck_agreement_status"),
        CheckConstraint("discount_percent BETWEEN 0 AND 100", name="ck_agreement_discount_pct"),
        CheckConstraint(
            "setup_fee_cents >= 0 AND recurring_fee_cents >= 0 AND discount_cents >= 0", name="ck_agreement_amounts"
        ),
        CheckConstraint("billing_frequency in ('monthly','quarterly','annual','one_time')", name="ck_agreement_freq"),
    )


class Invoice(IdMixin, TimestampMixin, Base):
    __tablename__ = "invoices"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    agreement_id: Mapped[int | None] = mapped_column(ForeignKey("client_agreements.id", ondelete="SET NULL"))
    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    # draft (not yet revenue) | issued | void | written_off. Paid state is derived from payments.
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)
    void_reason: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.id"
    )
    payments: Mapped[list[Payment]] = relationship(back_populates="invoice")
    credits: Mapped[list[CreditNote]] = relationship(back_populates="invoice")

    __table_args__ = (
        CheckConstraint("status in ('draft','issued','void','written_off')", name="ck_invoice_status"),
        CheckConstraint("due_date >= issue_date", name="ck_invoice_dates"),
    )


class InvoiceLine(IdMixin, Base):
    __tablename__ = "invoice_lines"
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    # setup | recurring | one_time | discount
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")

    __table_args__ = (
        CheckConstraint("kind in ('setup','recurring','one_time','discount')", name="ck_invoice_line_kind"),
        CheckConstraint("quantity > 0", name="ck_invoice_line_qty"),
        # Discounts are negative, everything else non-negative.
        CheckConstraint(
            "(kind = 'discount' AND unit_amount_cents <= 0) OR (kind <> 'discount' AND unit_amount_cents >= 0)",
            name="ck_invoice_line_sign",
        ),
    )

    @property
    def amount_cents(self) -> int:
        return self.quantity * self.unit_amount_cents


class Payment(IdMixin, TimestampMixin, Base):
    """A payment received. Only `confirmed` payments count as collected cash."""

    __tablename__ = "payments"
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
    reconciled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)

    invoice: Mapped[Invoice | None] = relationship(back_populates="payments")

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payment_amount"),
        CheckConstraint("status in ('pending','confirmed','failed','reversed')", name="ck_payment_status"),
        CheckConstraint("status <> 'confirmed' OR received_date IS NOT NULL", name="ck_payment_confirmed_date"),
        CheckConstraint(
            "method in ('ach','wire','check','cash','card_via_processor','processor_payout','other')",
            name="ck_payment_method",
        ),
    )


class CreditNote(IdMixin, TimestampMixin, Base):
    """Refunds (cash returned) and credits (amount forgiven) against a client/invoice."""

    __tablename__ = "credit_notes"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # refund | credit
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    issued_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True)

    invoice: Mapped[Invoice | None] = relationship(back_populates="credits")

    __table_args__ = (
        CheckConstraint("kind in ('refund','credit')", name="ck_credit_kind"),
        CheckConstraint("amount_cents > 0", name="ck_credit_amount"),
    )


class ExpenseCategory(IdMixin, Base):
    __tablename__ = "expense_categories"
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Counts toward client-acquisition cost (CAC / cost per lead / per meeting).
    is_acquisition: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Expense(IdMixin, TimestampMixin, Base):
    __tablename__ = "expenses"
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
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    category: Mapped[ExpenseCategory] = relationship()

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_expense_amount"),
        CheckConstraint("recurrence in ('one_time','monthly','quarterly','annual')", name="ck_expense_recurrence"),
        CheckConstraint(
            "reimbursement_status in ('not_applicable','pending','reimbursed','rejected')",
            name="ck_expense_reimbursement",
        ),
    )


class BankTransaction(IdMixin, Base):
    """Imported bank-statement line used for reconciliation."""

    __tablename__ = "bank_transactions"
    posted_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # + inflow, - outflow
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    import_batch: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    matched_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    matched_expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id", ondelete="SET NULL"))
    matched_credit_id: Mapped[int | None] = mapped_column(ForeignKey("credit_notes.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="unmatched", nullable=False)  # unmatched|matched|ignored
