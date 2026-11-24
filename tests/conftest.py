"""In-memory stand-ins for the HTTP clients.

They only implement what the flows call, and keep track of every write so tests can
check exactly what would have been sent.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from p2p_sync.connectors.erp import APInvoice, APInvoiceDraft, ClosedPeriodError, Vendor, VendorDraft
from p2p_sync.connectors.procurement import IntegrationStatus, Invoice, Supplier, SupplierStatus
from p2p_sync.core.errors import P2PSyncError


class FakeProcurement:
    def __init__(self) -> None:
        self.supplier_list: list[Supplier] = []
        self.invoice_list: list[Invoice] = []
        self.exported: list[int] = []
        self.exported_invoices: list[int] = []
        self.statuses: dict[tuple[str, int], tuple[IntegrationStatus, str]] = {}

    def approved_invoices(self, *, include_exported: bool = False) -> list[Invoice]:
        return [i for i in self.invoice_list if include_exported or i.id not in self.exported_invoices]

    def mark_invoice_exported(self, invoice_id: int) -> None:
        self.exported_invoices.append(invoice_id)

    def suppliers(
        self, *, status: SupplierStatus = SupplierStatus.ACTIVE, include_exported: bool = False
    ) -> list[Supplier]:
        return [s for s in self.supplier_list if s.status is status and (include_exported or not s.exported)]

    def mark_supplier_exported(self, supplier_id: int) -> None:
        self.exported.append(supplier_id)

    def set_integration_status(
        self, kind: str, record_id: int, status: IntegrationStatus, message: str = ""
    ) -> None:
        self.statuses[(kind, record_id)] = (status, message)


class FakeErp:
    def __init__(self) -> None:
        self.vendor_list: list[Vendor] = []
        self.created: list[VendorDraft] = []
        self.updated: list[tuple[str, VendorDraft]] = []
        self.ap_invoice_list: list[APInvoice] = []
        self.posted: list[APInvoiceDraft] = []
        self.closed_before: date | None = None  # periods before this date are closed

    def find_ap_invoice(self, vendor_id: str, invoice_number: str) -> APInvoice | None:
        found = [
            i for i in self.ap_invoice_list if i.vendor_id == vendor_id and i.invoice_number == invoice_number
        ]
        if len(found) > 1:
            raise P2PSyncError(f"{len(found)} AP invoices for vendor {vendor_id} number {invoice_number}")
        return found[0] if found else None

    def create_ap_invoice(self, draft: APInvoiceDraft) -> APInvoice:
        if self.closed_before and draft.invoice_date < self.closed_before:
            raise ClosedPeriodError(f"period {draft.invoice_date:%Y/%m} is closed ({draft.invoice_date})")
        invoice = APInvoice(**draft.model_dump(), invoice_id=500 + len(self.ap_invoice_list))
        self.ap_invoice_list.append(invoice)
        self.posted.append(draft)
        return invoice

    def vendors(self, *, active_only: bool = True) -> list[Vendor]:
        return [v for v in self.vendor_list if v.active or not active_only]

    def search_vendors(self, *, tax_id: str | None = None, external_ref: str | None = None) -> list[Vendor]:
        return [v for v in self.vendor_list if tax_id and v.tax_id == tax_id]

    def create_vendor(self, draft: VendorDraft) -> Vendor:
        vendor = Vendor(**draft.model_dump(), vendor_id=f"V{len(self.vendor_list) + 1:03d}")
        self.vendor_list.append(vendor)
        self.created.append(draft)
        return vendor

    def update_vendor(self, vendor_id: str, draft: VendorDraft) -> Vendor:
        vendor = Vendor(**draft.model_dump(), vendor_id=vendor_id)
        self.vendor_list = [vendor if v.vendor_id == vendor_id else v for v in self.vendor_list]
        self.updated.append((vendor_id, draft))
        return vendor


@pytest.fixture
def procurement() -> FakeProcurement:
    return FakeProcurement()


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp()


@pytest.fixture
def make_supplier() -> Callable[..., Supplier]:
    def make(supplier_id: int, **overrides: Any) -> Supplier:
        fields: dict[str, Any] = {
            "id": supplier_id,
            "name": f"Supplier {supplier_id}",
            "tax_id": f"TAX-{supplier_id}",
            "status": "active",
            "email": f"ap{supplier_id}@example.com",
            "payment_terms": "Net 30",
            "updated_at": datetime(2026, 11, 1, tzinfo=UTC),
        }
        fields.update(overrides)
        return Supplier.model_validate(fields)

    return make


@pytest.fixture
def make_invoice() -> Callable[..., Invoice]:
    def make(invoice_id: int, supplier_id: int = 1, **overrides: Any) -> Invoice:
        fields: dict[str, Any] = {
            "id": invoice_id,
            "invoice_number": f"INV-{invoice_id}",
            "supplier": {"id": supplier_id, "name": f"Supplier {supplier_id}"},
            "status": "approved",
            "invoice_date": date(2026, 10, 15),
            "currency": "PHP",
            "subtotal": Decimal("300.00"),
            "tax_amount": Decimal("36.00"),
            "total": Decimal("336.00"),
            "lines": [
                {
                    "line_number": 1,
                    "description": "Pump seal kit",
                    "quantity": 2,
                    "unit_price": "100.00",
                    "amount": "200.00",
                    "account_code": "6100",
                    "work_order_number": "WO-1001",
                },
                {
                    "line_number": 2,
                    "description": "Labour",
                    "quantity": 1,
                    "unit_price": "100.00",
                    "amount": "100.00",
                },
            ],
            "updated_at": datetime(2026, 11, 1, tzinfo=UTC),
        }
        fields.update(overrides)
        return Invoice.model_validate(fields)

    return make
