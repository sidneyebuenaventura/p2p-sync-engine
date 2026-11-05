from datetime import date
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import Numeric, String
from sqlalchemy.dialects import postgresql, sqlite

from p2p_sync.connectors.maintenance import (
    ContractorRepository,
    ContractorRow,
    CostRow,
    MaintenanceDatabase,
    WorkOrderRepository,
)
from p2p_sync.connectors.maintenance import repository as repository_module
from p2p_sync.connectors.maintenance.tables import Money
from p2p_sync.core.settings import MaintenanceSettings


def cost(source_ref: str, amount: str = "100.00") -> CostRow:
    return CostRow(
        source_ref=source_ref,
        description="Parts",
        amount=Decimal(amount),
        currency="PHP",
        incurred_on=date(2026, 10, 15),
    )


def test_session_commits_when_block_finishes(db: MaintenanceDatabase) -> None:
    with db.session() as session:
        session.add(ContractorRow(code="C-1", name="Acme Pumps"))

    with db.session() as session:
        assert [c.code for c in ContractorRepository(session).all()] == ["C-1"]


def test_session_rolls_back_when_block_raises(db: MaintenanceDatabase) -> None:
    def add_then_fail() -> None:
        with db.session() as session:
            session.add(ContractorRow(code="C-1", name="Acme Pumps"))
            session.flush()
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        add_then_fail()

    with db.session() as session:
        assert ContractorRepository(session).all() == []


def test_from_settings_uses_database_url() -> None:
    db = MaintenanceDatabase.from_settings(MaintenanceSettings(database_url=SecretStr("sqlite://")))

    assert db.engine.dialect.name == "sqlite"


def test_contractors_are_sorted_by_code_and_active_by_default(seeded: MaintenanceDatabase) -> None:
    with seeded.session() as session:
        repo = ContractorRepository(session)

        assert [c.code for c in repo.all()] == ["C-001", "C-002"]
        assert [c.code for c in repo.all(active_only=False)] == ["C-001", "C-002", "C-003"]


def test_contractor_lookups(seeded: MaintenanceDatabase) -> None:
    with seeded.session() as session:
        repo = ContractorRepository(session)
        linked = repo.by_supplier_id(10)

        assert linked is not None
        assert linked.name == "Acme Pumps"
        assert repo.by_supplier_id(999) is None
        assert [c.code for c in repo.by_tax_id("111")] == ["C-001", "C-003"]
        assert repo.get(linked.id) is linked


def test_add_contractor_assigns_id(db: MaintenanceDatabase) -> None:
    with db.session() as session:
        row = ContractorRepository(session).add(ContractorRow(code="C-9", name="New Co"))

        assert row.id is not None


def test_work_order_by_number(seeded: MaintenanceDatabase) -> None:
    with seeded.session() as session:
        repo = WorkOrderRepository(session)
        found = repo.by_number("WO-1001")

        assert found is not None
        assert found.description == "Replace pump seal"
        assert repo.by_number("WO-404") is None


def test_by_numbers_queries_in_chunks(seeded: MaintenanceDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_module, "_IN_CHUNK", 2)

    with seeded.session() as session:
        found = WorkOrderRepository(session).by_numbers(
            ["WO-1001", "WO-1002", "WO-0900", "WO-404", "WO-1001"]
        )

    assert sorted(found) == ["WO-0900", "WO-1001", "WO-1002"]


def test_by_numbers_with_nothing_to_look_up(seeded: MaintenanceDatabase) -> None:
    with seeded.session() as session:
        assert WorkOrderRepository(session).by_numbers([]) == {}


def test_open_orders_skip_completed_and_cancelled(seeded: MaintenanceDatabase) -> None:
    with seeded.session() as session:
        assert [wo.number for wo in WorkOrderRepository(session).open_orders()] == ["WO-1001", "WO-1002"]


def test_add_cost_is_idempotent_by_source_ref(seeded: MaintenanceDatabase) -> None:
    with seeded.session() as session:
        repo = WorkOrderRepository(session)
        work_order = repo.by_number("WO-1001")
        assert work_order is not None

        assert repo.add_cost(work_order, cost("procurement:invoice:1:line:1")) is True
        assert repo.add_cost(work_order, cost("procurement:invoice:1:line:1")) is False
        assert repo.cost_exists("procurement:invoice:1:line:1")

    with seeded.session() as session:
        reloaded = WorkOrderRepository(session).by_numbers(["WO-1001"])["WO-1001"]
        assert len(reloaded.costs) == 1


def test_money_keeps_exact_cents_on_sqlite(seeded: MaintenanceDatabase) -> None:
    with seeded.session() as session:
        repo = WorkOrderRepository(session)
        work_order = repo.by_number("WO-1002")
        assert work_order is not None
        for n in range(3):
            repo.add_cost(work_order, cost(f"ref-{n}", "0.10"))

    with seeded.session() as session:
        reloaded = WorkOrderRepository(session).by_numbers(["WO-1002"])["WO-1002"]
        assert sum(c.amount for c in reloaded.costs) == Decimal("0.30")


def test_money_uses_numeric_outside_sqlite() -> None:
    money = Money()

    assert isinstance(money.load_dialect_impl(postgresql.dialect()), Numeric)
    assert isinstance(money.load_dialect_impl(sqlite.dialect()), String)
    assert money.process_bind_param(Decimal("1.50"), postgresql.dialect()) == Decimal("1.50")
    assert money.process_bind_param(None, sqlite.dialect()) is None
    assert money.process_result_value(None, sqlite.dialect()) is None
