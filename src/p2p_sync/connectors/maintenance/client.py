"""Client for the maintenance system, backed by its database."""

from __future__ import annotations

from collections.abc import Iterable

from p2p_sync.connectors.maintenance.models import Contractor, ContractorDraft, WorkOrder, WorkOrderCost
from p2p_sync.connectors.maintenance.repository import (
    ContractorRepository,
    MaintenanceDatabase,
    WorkOrderRepository,
)
from p2p_sync.connectors.maintenance.tables import ContractorRow, CostRow, WorkOrderStatus
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.settings import MaintenanceSettings


class WorkOrderError(P2PSyncError):
    """A cost can't be booked against the work order (missing or cancelled)."""


class MaintenanceClient:
    # Every method is its own transaction, so a failure part way through a run leaves
    # earlier records committed, the same as with the HTTP clients.

    def __init__(self, db: MaintenanceDatabase) -> None:
        self.db = db

    @classmethod
    def from_settings(cls, settings: MaintenanceSettings) -> MaintenanceClient:
        return cls(MaintenanceDatabase.from_settings(settings))

    # contractors

    def contractors(self, *, active_only: bool = True) -> list[Contractor]:
        with self.db.session() as session:
            rows = ContractorRepository(session).all(active_only=active_only)
            return [Contractor.model_validate(row) for row in rows]

    def contractor_for_supplier(self, supplier_id: int) -> Contractor | None:
        with self.db.session() as session:
            row = ContractorRepository(session).by_supplier_id(supplier_id)
            return Contractor.model_validate(row) if row else None

    def create_contractor(self, draft: ContractorDraft) -> Contractor:
        with self.db.session() as session:
            row = ContractorRepository(session).add(ContractorRow(**draft.model_dump()))
            return Contractor.model_validate(row)

    def update_contractor(self, contractor_id: int, draft: ContractorDraft) -> Contractor:
        with self.db.session() as session:
            row = ContractorRepository(session).get(contractor_id)
            if row is None:
                raise P2PSyncError(f"contractor {contractor_id} not found")
            # only draft fields, even if handed a full Contractor, so the id can't change
            for field, value in draft.model_dump(include=set(ContractorDraft.model_fields)).items():
                setattr(row, field, value)
            session.flush()
            return Contractor.model_validate(row)

    # work orders

    def work_order(self, number: str) -> WorkOrder | None:
        with self.db.session() as session:
            row = WorkOrderRepository(session).by_number(number)
            return WorkOrder.model_validate(row) if row else None

    def work_orders(self, numbers: Iterable[str]) -> dict[str, WorkOrder]:
        with self.db.session() as session:
            rows = WorkOrderRepository(session).by_numbers(numbers)
            return {number: WorkOrder.model_validate(row) for number, row in rows.items()}

    def open_work_orders(self) -> list[WorkOrder]:
        with self.db.session() as session:
            return [WorkOrder.model_validate(row) for row in WorkOrderRepository(session).open_orders()]

    def book_cost(self, work_order_number: str, cost: WorkOrderCost) -> bool:
        """Book a cost on a work order. Returns False if it was already booked."""
        with self.db.session() as session:
            repo = WorkOrderRepository(session)
            row = repo.by_number(work_order_number)
            if row is None:
                raise WorkOrderError(f"work order {work_order_number} not found")
            # Invoices often turn up after the job is done, so completed work orders still
            # take costs. A cancelled one shouldn't have been invoiced at all.
            if row.status is WorkOrderStatus.CANCELLED:
                raise WorkOrderError(f"work order {work_order_number} is cancelled")
            return repo.add_cost(row, CostRow(**cost.model_dump()))
