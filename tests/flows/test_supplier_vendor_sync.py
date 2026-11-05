from collections.abc import Callable
from typing import Any

import pytest

from p2p_sync.connectors.erp import Vendor
from p2p_sync.connectors.procurement import IntegrationStatus, Supplier
from p2p_sync.flows import MappingError, SupplierVendorSync, suppliers_to_sync, vendor_draft
from p2p_sync.sync import SyncAction

MakeSupplier = Callable[..., Supplier]


@pytest.fixture
def sync(procurement: Any, erp: Any) -> SupplierVendorSync:
    return SupplierVendorSync(procurement, erp)


def test_vendor_draft_maps_address_bank_and_terms(make_supplier: MakeSupplier) -> None:
    supplier = make_supplier(
        7,
        payment_terms="  NET   45 ",
        remit_to={"line1": "1 Pump St", "city": "Makati", "state": "NCR", "country": "PH"},
        bank_account={"account_name": "Acme Pumps", "account_number": "0012345"},
    )

    draft = vendor_draft(supplier)

    assert draft.payment_terms_code == "N45"
    assert draft.address is not None
    assert draft.address.region == "NCR"
    assert draft.bank is not None
    assert draft.bank.account_number == "0012345"
    assert draft.external_ref == "procurement:supplier:7"
    assert draft.active is True


def test_vendor_draft_refuses_unknown_payment_terms(make_supplier: MakeSupplier) -> None:
    with pytest.raises(MappingError, match="no ERP payment terms code for 'Net 90'"):
        vendor_draft(make_supplier(1, payment_terms="Net 90"))

    assert vendor_draft(make_supplier(1, payment_terms=None)).payment_terms_code is None


def test_suppliers_to_sync_skips_inactive_ones_never_exported(
    procurement: Any, make_supplier: MakeSupplier
) -> None:
    procurement.supplier_list = [
        make_supplier(1),
        make_supplier(2, status="inactive", exported=True),
        make_supplier(3, status="inactive"),
        make_supplier(4, status="draft"),
    ]

    assert [s.id for s in suppliers_to_sync(procurement)] == [1, 2]


def test_new_supplier_creates_vendor_and_marks_exported(
    sync: SupplierVendorSync, procurement: Any, erp: Any, make_supplier: MakeSupplier
) -> None:
    procurement.supplier_list = [make_supplier(1)]

    report = sync.run()

    assert report.counts()["create"] == 1
    assert [d.external_ref for d in erp.created] == ["procurement:supplier:1"]
    assert procurement.exported == [1]
    assert procurement.statuses[("suppliers", 1)] == (IntegrationStatus.SUCCESS, "ERP vendor V001")


def test_second_run_changes_nothing(
    sync: SupplierVendorSync, procurement: Any, erp: Any, make_supplier: MakeSupplier
) -> None:
    procurement.supplier_list = [make_supplier(1)]
    sync.run()
    procurement.supplier_list = [make_supplier(1, exported=True)]

    report = sync.run()

    assert report.counts()["skip"] == 1
    assert len(erp.created) == 1
    assert erp.updated == []


def test_changed_supplier_updates_vendor(
    sync: SupplierVendorSync, procurement: Any, erp: Any, make_supplier: MakeSupplier
) -> None:
    sync.run([make_supplier(1)])

    report = sync.run([make_supplier(1, email="billing@example.com", exported=True)])

    assert report.counts()["update"] == 1
    assert erp.updated[0][1].email == "billing@example.com"
    assert procurement.exported == [1]  # not marked a second time


def test_inactive_supplier_switches_vendor_off(
    sync: SupplierVendorSync, procurement: Any, erp: Any, make_supplier: MakeSupplier
) -> None:
    sync.run([make_supplier(1)])
    procurement.supplier_list = [make_supplier(1, status="inactive", exported=True)]

    sync.run()

    assert erp.vendor_list[0].active is False


def test_takes_over_hand_made_vendor_with_same_tax_id(
    sync: SupplierVendorSync, erp: Any, make_supplier: MakeSupplier
) -> None:
    erp.vendor_list = [Vendor(vendor_id="V900", name="ACME PUMPS INC", tax_id="TAX-1")]

    report = sync.run([make_supplier(1)])

    assert report.counts()["create"] == 1
    assert erp.created == []
    assert erp.updated[0][0] == "V900"
    assert erp.vendor_list[0].external_ref == "procurement:supplier:1"


def test_supplier_without_tax_id_always_gets_a_new_vendor(
    sync: SupplierVendorSync, erp: Any, make_supplier: MakeSupplier
) -> None:
    erp.vendor_list = [Vendor(vendor_id="V900", name="Hand made, no tax id")]

    sync.run([make_supplier(1, tax_id=None)])

    assert len(erp.created) == 1
    assert erp.updated == []


def test_several_hand_made_vendors_is_a_failure_not_a_guess(
    sync: SupplierVendorSync, procurement: Any, erp: Any, make_supplier: MakeSupplier
) -> None:
    erp.vendor_list = [
        Vendor(vendor_id="V900", name="Acme", tax_id="TAX-1"),
        Vendor(vendor_id="V901", name="Acme Pumps", tax_id="TAX-1"),
    ]

    report = sync.run([make_supplier(1)])

    assert len(report.failures) == 1
    status, message = procurement.statuses[("suppliers", 1)]
    assert status is IntegrationStatus.ERROR
    assert "2 unlinked ERP vendors have tax id TAX-1" in message


def test_unmappable_supplier_fails_alone(
    sync: SupplierVendorSync, procurement: Any, erp: Any, make_supplier: MakeSupplier
) -> None:
    sync.run([make_supplier(2)])

    report = sync.run([make_supplier(1, payment_terms="Net 90"), make_supplier(2, payment_terms="Net 90")])

    # 1 fails on create, 2 is already linked and fails on update; neither stops the run
    assert [(r.failed_stage, type(r.error)) for r in report.results] == [
        ("create", MappingError),
        ("update", MappingError),
    ]
    assert procurement.statuses[("suppliers", 1)][0] is IntegrationStatus.ERROR


def test_conflict_is_reported_back_to_procurement(
    sync: SupplierVendorSync, procurement: Any, erp: Any, make_supplier: MakeSupplier
) -> None:
    ref = "procurement:supplier:1"
    erp.vendor_list = [
        Vendor(vendor_id="V1", name="A", external_ref=ref),
        Vendor(vendor_id="V2", name="B", external_ref=ref),
    ]

    report = sync.run([make_supplier(1)])

    assert report.results[0].action is SyncAction.CONFLICT
    assert "2 target records share key" in procurement.statuses[("suppliers", 1)][1]


def test_dry_run_writes_nothing(procurement: Any, erp: Any, make_supplier: MakeSupplier) -> None:
    report = SupplierVendorSync(procurement, erp, dry_run=True).run([make_supplier(1)])

    assert report.summary().startswith("[dry run] create=1")
    assert erp.created == []
    assert procurement.exported == []
    assert procurement.statuses == {}
