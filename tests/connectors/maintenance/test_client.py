from datetime import date
from decimal import Decimal

import pytest
from pydantic import SecretStr

from p2p_sync.connectors.maintenance import (
    ContractorDraft,
    MaintenanceClient,
    MaintenanceDatabase,
    WorkOrderCost,
    WorkOrderError,
    WorkOrderRepository,
    WorkOrderStatus,
)
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.settings import MaintenanceSettings


@pytest.fixture
def client(seeded: MaintenanceDatabase) -> MaintenanceClient:
    return MaintenanceClient(seeded)


def cost(ref: str, amount: str) -> WorkOrderCost:
    return WorkOrderCost(
        source_ref=ref,
        description="Parts",
        amount=Decimal(amount),
        currency="PHP",
        incurred_on=date(2026, 10, 9),
    )


def test_contractors_come_back_as_models(client: MaintenanceClient) -> None:
    contractors = client.contractors()

    assert [(c.code, c.procurement_supplier_id) for c in contractors] == [("C-001", 10), ("C-002", 20)]
    assert len(client.contractors(active_only=False)) == 3


def test_contractor_for_supplier(client: MaintenanceClient) -> None:
    found = client.contractor_for_supplier(20)

    assert found is not None
    assert found.name == "Beta Electrical"
    assert client.contractor_for_supplier(999) is None


def test_create_then_update_contractor(client: MaintenanceClient) -> None:
    created = client.create_contractor(
        ContractorDraft(code="C-010", name="Gamma HVAC", procurement_supplier_id=30)
    )
    updated = client.update_contractor(
        created.id, created.model_copy(update={"name": "Gamma HVAC Services", "email": "ap@gamma.example"})
    )

    assert updated.id == created.id
    assert updated.name == "Gamma HVAC Services"
    found = client.contractor_for_supplier(30)
    assert found is not None
    assert found.email == "ap@gamma.example"


def test_update_unknown_contractor(client: MaintenanceClient) -> None:
    with pytest.raises(P2PSyncError, match="contractor 999 not found"):
        client.update_contractor(999, ContractorDraft(code="X", name="X"))


def test_work_order_lookups(client: MaintenanceClient) -> None:
    single = client.work_order("WO-1001")

    assert single is not None
    assert single.status is WorkOrderStatus.OPEN
    assert client.work_order("WO-404") is None
    assert sorted(client.work_orders(["WO-1001", "WO-0900", "WO-404"])) == ["WO-0900", "WO-1001"]
    assert [wo.number for wo in client.open_work_orders()] == ["WO-1001", "WO-1002"]


def test_book_cost_adds_up_and_skips_repeats(client: MaintenanceClient) -> None:
    assert client.book_cost("WO-1001", cost("inv:1:line:1", "120.50")) is True
    assert client.book_cost("WO-1001", cost("inv:1:line:2", "79.50")) is True
    assert client.book_cost("WO-1001", cost("inv:1:line:1", "120.50")) is False

    work_order = client.work_order("WO-1001")
    assert work_order is not None
    assert work_order.actual_cost == Decimal("200.00")
    assert [c.source_ref for c in work_order.costs] == ["inv:1:line:1", "inv:1:line:2"]


def test_completed_work_orders_still_take_late_invoices(client: MaintenanceClient) -> None:
    assert client.book_cost("WO-0900", cost("inv:2:line:1", "50.00")) is True


def test_book_cost_refuses_cancelled_or_missing_work_orders(client: MaintenanceClient) -> None:
    with pytest.raises(WorkOrderError, match="WO-0901 is cancelled"):
        client.book_cost("WO-0901", cost("inv:3:line:1", "10.00"))
    with pytest.raises(WorkOrderError, match="WO-404 not found"):
        client.book_cost("WO-404", cost("inv:3:line:2", "10.00"))


def test_over_estimate(seeded: MaintenanceDatabase, client: MaintenanceClient) -> None:
    with seeded.session() as session:
        row = WorkOrderRepository(session).by_number("WO-1002")
        assert row is not None
        row.estimated_cost = Decimal("100.00")

    client.book_cost("WO-1002", cost("inv:4:line:1", "100.00"))
    at_estimate = client.work_order("WO-1002")
    client.book_cost("WO-1002", cost("inv:4:line:2", "0.01"))
    over = client.work_order("WO-1002")

    assert at_estimate is not None
    assert over is not None
    assert not at_estimate.over_estimate
    assert over.over_estimate


def test_from_settings() -> None:
    client = MaintenanceClient.from_settings(MaintenanceSettings(database_url=SecretStr("sqlite://")))

    assert client.db.engine.dialect.name == "sqlite"
