from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from p2p_sync.connectors.maintenance import ContractorRow, MaintenanceDatabase, WorkOrderRow, WorkOrderStatus


@pytest.fixture
def db(tmp_path: Path) -> Iterator[MaintenanceDatabase]:
    database = MaintenanceDatabase(f"sqlite:///{tmp_path / 'maintenance.db'}")
    database.create_schema()
    yield database
    database.engine.dispose()  # Windows won't delete the file while a connection is open


@pytest.fixture
def seeded(db: MaintenanceDatabase) -> MaintenanceDatabase:
    with db.session() as session:
        session.add_all(
            [
                ContractorRow(code="C-002", name="Beta Electrical", tax_id="222", procurement_supplier_id=20),
                ContractorRow(code="C-001", name="Acme Pumps", tax_id="111", procurement_supplier_id=10),
                ContractorRow(code="C-003", name="Old Plumbing Co", tax_id="111", active=False),
                WorkOrderRow(number="WO-1001", description="Replace pump seal", opened_on=date(2026, 10, 1)),
                WorkOrderRow(
                    number="WO-1002",
                    description="Rewire panel",
                    status=WorkOrderStatus.IN_PROGRESS,
                    opened_on=date(2026, 10, 2),
                ),
                WorkOrderRow(
                    number="WO-0900",
                    description="Annual inspection",
                    status=WorkOrderStatus.COMPLETED,
                    opened_on=date(2026, 9, 1),
                    closed_on=date(2026, 9, 30),
                ),
                WorkOrderRow(
                    number="WO-0901",
                    description="Duplicate request",
                    status=WorkOrderStatus.CANCELLED,
                    opened_on=date(2026, 9, 5),
                ),
            ]
        )
    return db
