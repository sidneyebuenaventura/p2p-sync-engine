"""Database access for the maintenance system."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from p2p_sync.connectors.maintenance.tables import (
    Base,
    ContractorRow,
    CostRow,
    WorkOrderRow,
    WorkOrderStatus,
)
from p2p_sync.core.settings import MaintenanceSettings

# SQL Server allows about 2100 parameters per statement, so big IN lists go in chunks.
_IN_CHUNK = 500


class MaintenanceDatabase:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine = create_engine(url, echo=echo, pool_pre_ping=True)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: MaintenanceSettings) -> MaintenanceDatabase:
        return cls(settings.database_url.get_secret_value())

    def create_schema(self) -> None:
        # for tests and the demo only; the real schema belongs to the maintenance system
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One unit of work: commits when the block finishes, rolls back if it raises."""
        with self._sessions.begin() as session:
            yield session


class ContractorRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, contractor_id: int) -> ContractorRow | None:
        return self.session.get(ContractorRow, contractor_id)

    def all(self, *, active_only: bool = True) -> list[ContractorRow]:
        query = select(ContractorRow).order_by(ContractorRow.code)
        if active_only:
            query = query.where(ContractorRow.active.is_(True))
        return list(self.session.scalars(query))

    def by_supplier_id(self, supplier_id: int) -> ContractorRow | None:
        query = select(ContractorRow).where(ContractorRow.procurement_supplier_id == supplier_id)
        return self.session.scalars(query).one_or_none()

    def by_tax_id(self, tax_id: str) -> list[ContractorRow]:
        query = select(ContractorRow).where(ContractorRow.tax_id == tax_id).order_by(ContractorRow.id)
        return list(self.session.scalars(query))

    def add(self, contractor: ContractorRow) -> ContractorRow:
        self.session.add(contractor)
        self.session.flush()  # so the caller gets the new id
        return contractor


class WorkOrderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def by_number(self, number: str) -> WorkOrderRow | None:
        query = select(WorkOrderRow).where(WorkOrderRow.number == number)
        return self.session.scalars(query).one_or_none()

    def by_numbers(self, numbers: Iterable[str]) -> dict[str, WorkOrderRow]:
        wanted = sorted(set(numbers))
        found: dict[str, WorkOrderRow] = {}
        for start in range(0, len(wanted), _IN_CHUNK):
            query = (
                select(WorkOrderRow)
                .where(WorkOrderRow.number.in_(wanted[start : start + _IN_CHUNK]))
                .options(selectinload(WorkOrderRow.costs))
            )
            found.update((row.number, row) for row in self.session.scalars(query))
        return found

    def open_orders(self) -> list[WorkOrderRow]:
        query = (
            select(WorkOrderRow)
            .where(WorkOrderRow.status.in_([WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS]))
            .options(selectinload(WorkOrderRow.costs))
            .order_by(WorkOrderRow.number)
        )
        return list(self.session.scalars(query))

    def cost_exists(self, source_ref: str) -> bool:
        query = select(CostRow.id).where(CostRow.source_ref == source_ref)
        return self.session.scalars(query).first() is not None

    def add_cost(self, work_order: WorkOrderRow, cost: CostRow) -> bool:
        """Attach a cost unless one with the same source_ref is already booked."""
        if self.cost_exists(cost.source_ref):
            return False
        work_order.costs.append(cost)
        self.session.flush()
        return True
