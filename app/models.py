from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .money import ZERO

Money = Numeric(14, 2)


def now():
    return datetime.now(timezone.utc)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str | None] = mapped_column(String(20), unique=True)  # INV-000001, set after insert
    customer: Mapped[str] = mapped_column(String(200))
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))  # percent
    subtotal: Mapped[Decimal] = mapped_column(Money)
    tax: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")  # DRAFT PENDING APPROVED REJECTED
    created_by: Mapped[str] = mapped_column(String(100))
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    lines: Mapped[list["LineItem"]] = relationship(cascade="all, delete-orphan", order_by="LineItem.id")
    credits: Mapped[list["Credit"]] = relationship()
    payments: Mapped[list["Payment"]] = relationship()

    @property
    def credited(self) -> Decimal:
        return sum((c.amount for c in self.credits if c.status == "APPROVED"), ZERO)

    @property
    def paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), ZERO)

    @property
    def balance(self) -> Decimal:
        return self.total - self.credited - self.paid


class LineItem(Base):
    __tablename__ = "line_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    description: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)


class Credit(Base):
    """A credit memo: reduces what the customer owes on an invoice. Needs approval to take effect."""
    __tablename__ = "credits"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    amount: Mapped[Decimal] = mapped_column(Money)  # always positive
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING APPROVED REJECTED
    created_by: Mapped[str] = mapped_column(String(100))
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("payment_id"),)  # DB-level duplicate guard
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(100))
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    paid_on: Mapped[date] = mapped_column(Date)
    reference: Mapped[str] = mapped_column(String(300))
    applied_by: Mapped[str] = mapped_column(String(100))


class AuditLog(Base):
    """Append-only. There is intentionally no update/delete code path anywhere in the app."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    actor: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(50))
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column()
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)


class Campaign(Base):
    """An insertion order: the customer books N impressions at a CPM over a flight. One invoice per campaign."""
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    customer: Mapped[str] = mapped_column(String(200))
    booked_impressions: Mapped[int] = mapped_column(BigInteger)
    cpm: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    flight_start: Mapped[date] = mapped_column(Date)
    flight_end: Mapped[date] = mapped_column(Date)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    created_by: Mapped[str] = mapped_column(String(100))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deliveries: Mapped[list["DeliveryDay"]] = relationship(order_by="DeliveryDay.day")

    @property
    def delivered(self) -> int:
        return sum(d.impressions for d in self.deliveries)


class DeliveryDay(Base):
    __tablename__ = "delivery_days"
    __table_args__ = (UniqueConstraint("campaign_id", "day"),)   # DB-level guard: one row per campaign per day
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    day: Mapped[date] = mapped_column(Date)
    impressions: Mapped[int] = mapped_column(BigInteger)
