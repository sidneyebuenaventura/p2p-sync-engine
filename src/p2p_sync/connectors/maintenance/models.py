"""Maintenance data as the rest of the code sees it.

The flows get these instead of ORM rows, so they can't hold a session open or set off a
lazy load by accident, and the maintenance client looks like the other two clients.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from p2p_sync.connectors.maintenance.tables import WorkOrderStatus


class _MaintenanceModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True, from_attributes=True)


class ContractorDraft(_MaintenanceModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=100)
    tax_id: str | None = None
    email: str | None = None
    active: bool = True
    procurement_supplier_id: int | None = None


class Contractor(ContractorDraft):
    id: int


class WorkOrderCost(_MaintenanceModel):
    source_ref: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=200)
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)
    incurred_on: date


class WorkOrder(_MaintenanceModel):
    id: int
    number: str
    description: str
    status: WorkOrderStatus
    asset_code: str | None = None
    contractor_id: int | None = None
    estimated_cost: Decimal | None = None
    opened_on: date
    closed_on: date | None = None
    costs: list[WorkOrderCost] = Field(default_factory=list)

    @property
    def actual_cost(self) -> Decimal:
        return sum((cost.amount for cost in self.costs), Decimal("0"))

    @property
    def over_estimate(self) -> bool:
        return self.estimated_cost is not None and self.actual_cost > self.estimated_cost
