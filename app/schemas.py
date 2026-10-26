from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Qty = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]  # 10.005 is rejected, not silently rounded
Amount = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


class LineItemIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    quantity: Qty
    unit_price: Price


class InvoiceIn(BaseModel):
    customer: str = Field(min_length=1, max_length=200)
    issue_date: date
    due_date: date
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100, max_digits=6, decimal_places=3)
    lines: list[LineItemIn] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def due_after_issue(self):
        if self.due_date < self.issue_date:
            raise ValueError("due_date cannot be before issue_date")
        return self


class RejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class CreditIn(BaseModel):
    amount: Amount          # credits are entered as positive numbers; negatives are rejected
    reason: str = Field(min_length=3, max_length=500)


class LineItemOut(LineItemIn):
    model_config = ConfigDict(from_attributes=True)
    line_total: Decimal


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    number: str
    customer: str
    status: str
    issue_date: date
    due_date: date
    tax_rate: Decimal
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    credited: Decimal
    paid: Decimal
    balance: Decimal
    created_by: str
    decided_by: str | None
    rejection_reason: str | None
    lines: list[LineItemOut]


class CreditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_id: int
    amount: Decimal
    reason: str
    status: str
    created_by: str
    decided_by: str | None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    actor: str
    role: str
    action: str
    entity_type: str
    entity_id: int
    before: dict | None
    after: dict | None


class ReconRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    line: int
    payment_id: str
    reference: str
    amount: Decimal | None
    paid_on: date | None
    status: str
    invoice_number: str | None
    balance_after: Decimal | None
    flags: list[str]
    message: str


class ReconOut(BaseModel):
    applied: bool
    summary: dict[str, int]
    rows: list[ReconRowOut]


# ---------------------------------------------------------------- campaigns / delivery
class CampaignIn(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")
    customer: str = Field(min_length=1, max_length=200)
    booked_impressions: int = Field(gt=0, le=10_000_000_000)
    cpm: Annotated[Decimal, Field(gt=0, max_digits=8, decimal_places=2)]
    flight_start: date
    flight_end: date
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100, max_digits=6, decimal_places=3)

    @model_validator(mode="after")
    def flight_order(self):
        if self.flight_end < self.flight_start:
            raise ValueError("flight_end cannot be before flight_start")
        return self


class CampaignOut(BaseModel):
    id: int
    code: str
    customer: str
    booked_impressions: int
    delivered_impressions: int
    delivery_pct: Decimal
    cpm: Decimal
    flight_start: date
    flight_end: date
    invoice_id: int
    invoice_number: str
    settled: bool


class DeliveryRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    line: int
    campaign: str
    day: date | None
    impressions: int | None
    status: str
    message: str


class AnomalyOut(BaseModel):
    campaign: str
    day: date
    flag: str
    impressions: int


class DeliveryOut(BaseModel):
    applied: bool
    summary: dict[str, int]
    rows: list[DeliveryRowOut]
    anomalies: list[AnomalyOut]


class SettleOut(BaseModel):
    campaign: str
    booked_impressions: int
    delivered_impressions: int
    delivery_pct: Decimal
    flags: list[str]
    credit: CreditOut | None
