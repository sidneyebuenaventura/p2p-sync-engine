from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from p2p_sync.connectors.maintenance import ContractorDraft, MaintenanceClient, MaintenanceDatabase
from p2p_sync.connectors.procurement import Supplier
from p2p_sync.flows import SupplierContractorSync

MakeSupplier = Callable[..., Supplier]


@pytest.fixture
def maintenance(tmp_path: Path) -> Iterator[MaintenanceClient]:
    db = MaintenanceDatabase(f"sqlite:///{tmp_path / 'maintenance.db'}")
    db.create_schema()
    yield MaintenanceClient(db)
    db.engine.dispose()


@pytest.fixture
def sync(procurement: Any, maintenance: MaintenanceClient) -> SupplierContractorSync:
    return SupplierContractorSync(procurement, maintenance)


def test_new_supplier_becomes_linked_contractor(
    sync: SupplierContractorSync, maintenance: MaintenanceClient, make_supplier: MakeSupplier
) -> None:
    report = sync.run([make_supplier(1)])

    assert report.counts()["create"] == 1
    contractor = maintenance.contractor_for_supplier(1)
    assert contractor is not None
    assert (contractor.code, contractor.name) == ("PS000001", "Supplier 1")


def test_rerun_skips_and_change_updates_but_keeps_code(
    sync: SupplierContractorSync, maintenance: MaintenanceClient, make_supplier: MakeSupplier
) -> None:
    sync.run([make_supplier(1)])
    contractor = maintenance.contractor_for_supplier(1)
    assert contractor is not None
    maintenance.update_contractor(contractor.id, contractor.model_copy(update={"code": "PUMPS"}))

    assert sync.run([make_supplier(1)]).counts()["skip"] == 1
    assert sync.run([make_supplier(1, name="Supplier One")]).counts()["update"] == 1

    updated = maintenance.contractor_for_supplier(1)
    assert updated is not None
    assert (updated.code, updated.name) == ("PUMPS", "Supplier One")


def test_takes_over_hand_made_contractor_with_same_tax_id(
    sync: SupplierContractorSync, maintenance: MaintenanceClient, make_supplier: MakeSupplier
) -> None:
    maintenance.create_contractor(ContractorDraft(code="ACME", name="acme pumps", tax_id="TAX-1"))

    sync.run([make_supplier(1)])

    [contractor] = maintenance.contractors()
    assert (contractor.code, contractor.procurement_supplier_id) == ("ACME", 1)


def test_several_hand_made_contractors_is_a_failure(
    sync: SupplierContractorSync, maintenance: MaintenanceClient, make_supplier: MakeSupplier
) -> None:
    maintenance.create_contractor(ContractorDraft(code="A1", name="Acme", tax_id="TAX-1"))
    maintenance.create_contractor(ContractorDraft(code="A2", name="Acme Pumps", tax_id="TAX-1"))

    report = sync.run([make_supplier(1), make_supplier(2)])

    assert [r.source.id for r in report.failures] == [1]
    assert "2 unlinked contractors have tax id TAX-1" in str(report.failures[0].error)
    assert maintenance.contractor_for_supplier(2) is not None


def test_only_wanted_suppliers_are_synced(
    procurement: Any, maintenance: MaintenanceClient, make_supplier: MakeSupplier
) -> None:
    sync = SupplierContractorSync(procurement, maintenance, wanted=lambda s: s.id == 2)

    sync.run([make_supplier(1), make_supplier(2)])

    assert [c.procurement_supplier_id for c in maintenance.contractors()] == [2]


def test_inactive_supplier_switches_off_its_contractor_only(
    sync: SupplierContractorSync,
    procurement: Any,
    maintenance: MaintenanceClient,
    make_supplier: MakeSupplier,
) -> None:
    sync.run([make_supplier(1)])
    procurement.supplier_list = [
        make_supplier(1, status="inactive", exported=True),
        make_supplier(2, status="inactive", exported=True),  # never had a contractor
    ]

    report = sync.run()

    assert [r.source.id for r in report.results] == [1]
    assert maintenance.contractors() == []
    assert len(maintenance.contractors(active_only=False)) == 1


def test_dry_run_writes_nothing(
    procurement: Any, maintenance: MaintenanceClient, make_supplier: MakeSupplier
) -> None:
    report = SupplierContractorSync(procurement, maintenance, dry_run=True).run([make_supplier(1)])

    assert report.counts()["create"] == 1
    assert maintenance.contractors(active_only=False) == []
