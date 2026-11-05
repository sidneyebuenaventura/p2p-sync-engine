"""In-memory stand-ins for the HTTP clients.

They only implement what the flows call, and keep track of every write so tests can
check exactly what would have been sent.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from p2p_sync.connectors.erp import Vendor, VendorDraft
from p2p_sync.connectors.procurement import IntegrationStatus, Supplier, SupplierStatus


class FakeProcurement:
    def __init__(self) -> None:
        self.supplier_list: list[Supplier] = []
        self.exported: list[int] = []
        self.statuses: dict[tuple[str, int], tuple[IntegrationStatus, str]] = {}

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
