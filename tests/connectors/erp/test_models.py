from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from p2p_sync.connectors.erp import APInvoice, APInvoiceDraft, FiscalPeriod, Vendor, VendorDraft


def line(number: int, amount: str) -> dict[str, Any]:
    return {"line_number": number, "gl_account": "6100", "description": "Parts", "amount": amount}


def draft(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "vendor_id": "V0012",
        "invoice_number": "INV-1001",
        "invoice_date": "2026-10-01",
        "currency": "PHP",
        "lines": [line(1, "1000.00"), line(2, "500.00")],
        "tax_amount": "180.00",
    }
    payload.update(overrides)
    return payload


def test_draft_computes_subtotal_and_total() -> None:
    invoice = APInvoiceDraft.model_validate(draft())

    assert invoice.subtotal == Decimal("1500.00")
    assert invoice.total == Decimal("1680.00")


def test_draft_rejects_duplicate_line_numbers() -> None:
    with pytest.raises(ValidationError, match="duplicate line numbers"):
        APInvoiceDraft.model_validate(draft(lines=[line(1, "10"), line(1, "20")]))


def test_draft_needs_a_line_and_a_gl_account() -> None:
    with pytest.raises(ValidationError):
        APInvoiceDraft.model_validate(draft(lines=[]))
    with pytest.raises(ValidationError):
        APInvoiceDraft.model_validate(draft(lines=[{**line(1, "10"), "gl_account": ""}]))


def test_read_model_carries_erp_owned_fields() -> None:
    invoice = APInvoice.model_validate({**draft(), "invoice_id": 9001, "posted": True})

    assert invoice.invoice_id == 9001
    assert invoice.posted


def test_draft_does_not_accept_erp_owned_fields() -> None:
    sent = APInvoiceDraft.model_validate({**draft(), "invoice_id": 9001})

    assert "invoice_id" not in sent.model_dump()


def test_vendor_draft_and_vendor() -> None:
    payload = {
        "name": "Acme Pumps",
        "tax_id": "123-456-789",
        "address": {"line1": "1 Main St", "city": "Makati", "country": "PH"},
        "bank": {"account_name": "Acme Pumps Inc", "account_number": "0012345678"},
        "external_ref": "12",
    }

    assert "vendor_id" not in VendorDraft.model_validate(payload).model_dump()
    vendor = Vendor.model_validate({**payload, "vendor_id": "V0012"})
    assert vendor.vendor_id == "V0012"
    assert vendor.active


def test_vendor_name_length_is_limited() -> None:
    with pytest.raises(ValidationError):
        VendorDraft(name="x" * 101)


@pytest.mark.parametrize(
    ("day", "inside"),
    [
        (date(2026, 9, 30), False),
        (date(2026, 10, 1), True),
        (date(2026, 10, 31), True),
        (date(2026, 11, 1), False),
    ],
)
def test_fiscal_period_contains(day: date, inside: bool) -> None:
    period = FiscalPeriod(
        year=2026, period=10, start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), closed=False
    )

    assert period.contains(day) is inside
