from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from p2p_sync.connectors.procurement import Invoice, InvoiceStatus, Supplier, SupplierStatus


def invoice_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 501,
        "invoice_number": "INV-1001",
        "supplier": {"id": 12, "name": "Acme Pumps"},
        "status": "approved",
        "invoice_date": "2026-10-01",
        "due_date": "2026-10-31",
        "currency": "PHP",
        "subtotal": "1500.00",
        "tax_amount": "180.00",
        "total": "1680.00",
        "lines": [
            {"line_number": 1, "description": "Pump seal kit", "quantity": "2", "unit_price": "500.00",
             "amount": "1000.00", "account_code": "6100"},
            {"line_number": 2, "description": "Call-out fee", "quantity": "1", "unit_price": "500.00",
             "amount": "500.00", "work_order_number": "WO-88"},
        ],
        "updated_at": "2026-10-02T09:30:00Z",
    }  # fmt: skip
    payload.update(overrides)
    return payload


def test_parses_invoice_with_decimals_and_enums() -> None:
    invoice = Invoice.model_validate(invoice_payload())

    assert invoice.status is InvoiceStatus.APPROVED
    assert invoice.total == Decimal("1680.00")
    assert isinstance(invoice.lines[0].quantity, Decimal)
    assert invoice.lines[1].work_order_number == "WO-88"
    assert invoice.exported is False


def test_ignores_unknown_fields() -> None:
    invoice = Invoice.model_validate(invoice_payload(new_field_from_vendor="whatever"))

    assert not hasattr(invoice, "new_field_from_vendor")


def test_models_are_immutable() -> None:
    invoice = Invoice.model_validate(invoice_payload())

    with pytest.raises(ValidationError):
        invoice.total = Decimal("1")  # type: ignore[misc]


def test_rejects_line_amount_that_does_not_match_quantity_times_price() -> None:
    payload = invoice_payload()
    payload["lines"][0]["amount"] = "999.00"

    with pytest.raises(ValidationError, match=r"line 1: amount 999.00 != 2 x 500.00"):
        Invoice.model_validate(payload)


def test_rejects_lines_not_adding_up_to_subtotal() -> None:
    with pytest.raises(ValidationError, match=r"lines add up to 1500\.00, subtotal is 1400\.00"):
        Invoice.model_validate(invoice_payload(subtotal="1400.00", total="1580.00"))


def test_rejects_wrong_total() -> None:
    with pytest.raises(ValidationError, match=r"subtotal 1500.00 \+ tax 180.00 != total 1700.00"):
        Invoice.model_validate(invoice_payload(total="1700.00"))


def test_tolerates_a_cent_of_rounding() -> None:
    invoice = Invoice.model_validate(invoice_payload(total="1680.01"))

    assert invoice.total == Decimal("1680.01")


def test_rejects_due_date_before_invoice_date() -> None:
    with pytest.raises(ValidationError, match="due date is before invoice date"):
        Invoice.model_validate(invoice_payload(due_date="2026-09-01"))


def test_requires_at_least_one_line() -> None:
    with pytest.raises(ValidationError, match="lines"):
        Invoice.model_validate(invoice_payload(lines=[], subtotal="0", tax_amount="0", total="0"))


def test_parses_supplier_with_nested_address_and_bank() -> None:
    supplier = Supplier.model_validate(
        {
            "id": 12,
            "name": "  Acme Pumps  ",
            "tax_id": "123-456-789",
            "status": "active",
            "remit_to": {"line1": "1 Main St", "city": "Makati", "country": "PH"},
            "bank_account": {"account_name": "Acme Pumps Inc", "account_number": "0012345678"},
            "updated_at": "2026-10-01T00:00:00+08:00",
        }
    )

    assert supplier.name == "Acme Pumps"
    assert supplier.status is SupplierStatus.ACTIVE
    assert supplier.remit_to is not None
    assert supplier.remit_to.country == "PH"
    assert supplier.bank_account is not None
    assert supplier.bank_account.account_number == "0012345678"
