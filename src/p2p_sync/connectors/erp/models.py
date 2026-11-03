"""Data models for the ERP's accounts payable API.

Read models (Vendor, APInvoice) carry the ids the ERP assigns. Write models
(VendorDraft, APInvoiceDraft) are what we send when creating or updating, so the
sync code can't accidentally post an ERP-owned field like a vendor id.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ErpModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class ErpAddress(_ErpModel):
    line1: str
    line2: str | None = None
    city: str
    region: str | None = None
    postal_code: str | None = None
    country: str = Field(min_length=2, max_length=2)


class VendorBank(_ErpModel):
    account_name: str
    account_number: str
    bank_name: str | None = None
    routing_code: str | None = None
    swift: str | None = None


class VendorDraft(_ErpModel):
    name: str = Field(min_length=1, max_length=100)
    tax_id: str | None = None
    email: str | None = None
    payment_terms_code: str | None = None
    address: ErpAddress | None = None
    bank: VendorBank | None = None
    active: bool = True
    # id of the record in the system that created the vendor, so we can find it again
    external_ref: str | None = None


class Vendor(VendorDraft):
    vendor_id: str


class APInvoiceLine(_ErpModel):
    line_number: int = Field(ge=1)
    gl_account: str = Field(min_length=1)
    description: str = Field(max_length=200)
    amount: Decimal
    tax_code: str | None = None


class APInvoiceDraft(_ErpModel):
    vendor_id: str
    invoice_number: str = Field(min_length=1, max_length=50)
    invoice_date: date
    due_date: date | None = None
    currency: str = Field(min_length=3, max_length=3)
    lines: list[APInvoiceLine] = Field(min_length=1)
    tax_amount: Decimal = Decimal("0")
    external_ref: str | None = None

    @property
    def subtotal(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax_amount

    @model_validator(mode="after")
    def _line_numbers_are_unique(self) -> APInvoiceDraft:
        numbers = [line.line_number for line in self.lines]
        if len(numbers) != len(set(numbers)):
            raise ValueError(f"invoice {self.invoice_number}: duplicate line numbers {numbers}")
        return self


class APInvoice(APInvoiceDraft):
    invoice_id: int
    posted: bool = False


class FiscalPeriod(_ErpModel):
    year: int
    period: int = Field(ge=1, le=13)  # some calendars have a 13th adjustment period
    start_date: date
    end_date: date
    closed: bool

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date


class VendorPayment(_ErpModel):
    payment_id: int
    invoice_id: int
    vendor_id: str
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    paid_on: date
    reference: str
