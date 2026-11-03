"""Data models for the procurement system's REST API.

Money is always Decimal. Unknown fields are ignored so new fields on the API side
don't break the sync, but the totals on an invoice are checked because posting an
invoice whose lines don't add up is exactly the kind of mistake we want to catch early.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Allow a cent of drift per line from rounding in the source system.
_ROUNDING_TOLERANCE = Decimal("0.01")


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class SupplierStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class IntegrationStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"


class Address(_ApiModel):
    line1: str
    line2: str | None = None
    city: str
    state: str | None = None
    postal_code: str | None = None
    country: str = Field(min_length=2, max_length=2)  # ISO 3166 alpha-2


class BankAccount(_ApiModel):
    account_name: str
    bank_name: str | None = None
    account_number: str
    routing_code: str | None = None  # BSB / sort code / ABA, depending on country
    swift: str | None = None


class Supplier(_ApiModel):
    id: int
    name: str
    tax_id: str | None = None
    status: SupplierStatus
    email: str | None = None
    payment_terms: str | None = None
    remit_to: Address | None = None
    bank_account: BankAccount | None = None
    exported: bool = False
    updated_at: datetime


class SupplierRef(_ApiModel):
    id: int
    name: str


class InvoiceLine(_ApiModel):
    line_number: int = Field(ge=1)
    description: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal
    amount: Decimal
    account_code: str | None = None
    tax_code: str | None = None
    work_order_number: str | None = None

    @model_validator(mode="after")
    def _amount_matches_quantity_times_price(self) -> InvoiceLine:
        expected = self.quantity * self.unit_price
        if abs(expected - self.amount) > _ROUNDING_TOLERANCE:
            raise ValueError(
                f"line {self.line_number}: amount {self.amount} != {self.quantity} x {self.unit_price}"
            )
        return self


class Invoice(_ApiModel):
    id: int
    invoice_number: str = Field(min_length=1)
    supplier: SupplierRef
    status: InvoiceStatus
    invoice_date: date
    due_date: date | None = None
    currency: str = Field(min_length=3, max_length=3)
    subtotal: Decimal
    tax_amount: Decimal = Decimal("0")
    total: Decimal
    lines: list[InvoiceLine] = Field(min_length=1)
    exported: bool = False
    updated_at: datetime

    @model_validator(mode="after")
    def _totals_add_up(self) -> Invoice:
        line_sum = sum((line.amount for line in self.lines), Decimal("0"))
        if abs(line_sum - self.subtotal) > _ROUNDING_TOLERANCE * len(self.lines):
            raise ValueError(
                f"invoice {self.invoice_number}: lines add up to {line_sum}, subtotal is {self.subtotal}"
            )
        if abs(self.subtotal + self.tax_amount - self.total) > _ROUNDING_TOLERANCE:
            raise ValueError(
                f"invoice {self.invoice_number}: "
                f"subtotal {self.subtotal} + tax {self.tax_amount} != total {self.total}"
            )
        if self.due_date and self.due_date < self.invoice_date:
            raise ValueError(f"invoice {self.invoice_number}: due date is before invoice date")
        return self


class Payment(_ApiModel):
    """A payment made in the ERP, written back onto the procurement invoice."""

    amount: Decimal = Field(gt=0)
    paid_on: date
    reference: str
    currency: str = Field(min_length=3, max_length=3)
