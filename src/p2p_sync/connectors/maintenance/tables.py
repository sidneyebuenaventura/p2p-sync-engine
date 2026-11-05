"""Tables in the maintenance system's database.

The maintenance system has no API we can use, so the integration reads and writes its
database directly. Only the tables and columns the sync touches are mapped here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, TypeEngine


class WorkOrderStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Money(TypeDecorator[Decimal]):
    # SQLite has no decimal type and would round-trip through float, so store text there.
    impl = Numeric(12, 2)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(20))
        return dialect.type_descriptor(Numeric(12, 2))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Any:
        if value is None or dialect.name != "sqlite":
            return value
        return str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


def _enum_values(enum: type[Enum]) -> list[str]:
    return [str(member.value) for member in enum]


class Base(DeclarativeBase):
    pass


class ContractorRow(Base):
    __tablename__ = "contractors"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    tax_id: Mapped[str | None] = mapped_column(String(30), index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    active: Mapped[bool] = mapped_column(default=True)
    # set once the contractor is linked to a supplier in the procurement system
    procurement_supplier_id: Mapped[int | None] = mapped_column(unique=True)


class WorkOrderRow(Base):
    __tablename__ = "work_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    description: Mapped[str] = mapped_column(String(200))
    status: Mapped[WorkOrderStatus] = mapped_column(
        SqlEnum(WorkOrderStatus, native_enum=False, length=20, values_callable=_enum_values),
        default=WorkOrderStatus.OPEN,
    )
    asset_code: Mapped[str | None] = mapped_column(String(30))
    contractor_id: Mapped[int | None] = mapped_column(ForeignKey("contractors.id"))
    estimated_cost: Mapped[Decimal | None] = mapped_column(Money())
    opened_on: Mapped[date]
    closed_on: Mapped[date | None]

    costs: Mapped[list[CostRow]] = relationship(
        back_populates="work_order", cascade="all, delete-orphan", order_by="CostRow.id"
    )


class CostRow(Base):
    __tablename__ = "work_order_costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_orders.id"), index=True)
    # Where the cost came from, e.g. "procurement:invoice:123:line:2". Unique, so a rerun
    # of the sync can't book the same invoice line twice.
    source_ref: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Money())
    currency: Mapped[str] = mapped_column(String(3))
    incurred_on: Mapped[date]

    work_order: Mapped[WorkOrderRow] = relationship(back_populates="costs")
