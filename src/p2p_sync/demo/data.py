"""Sample data for Acme Manufacturing. Each supplier and invoice is there to show one case.

Dates are relative to today so the demo behaves the same whenever it runs: this month
and last month are open in the ERP, anything older is closed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from p2p_sync.connectors.maintenance import ContractorRow, MaintenanceDatabase, WorkOrderRow, WorkOrderStatus
from p2p_sync.demo.erp_api import ErpStore
from p2p_sync.demo.procurement_api import ProcurementStore

VAT = Decimal("0.12")

ACME_PUMPS = 1
BETA_ELECTRICAL = 2
GAMMA_OFFICE = 3
DELTA_INDUSTRIAL = 4

# (description, quantity, unit price, account code, work order)
Line = tuple[str, int, str, str | None, str | None]


def procurement_store(today: date) -> ProcurementStore:
    stamp = datetime.combine(today, time(8, 0), UTC).isoformat()
    suppliers = [
        # new: becomes an ERP vendor and a maintenance contractor
        _supplier(ACME_PUMPS, "Acme Pumps Inc.", "201-555-001", "Net 30", stamp, with_bank=True),
        # AP and the maintenance team both set this one up by hand before the integration
        _supplier(BETA_ELECTRICAL, "Beta Electrical Services", "201-555-002", "Net 45", stamp),
        # the ERP has no payment terms code for Net 90, so this one fails
        _supplier(GAMMA_OFFICE, "Gamma Office Supplies", "201-555-003", "Net 90", stamp),
        # inactive and never sent anywhere: left alone
        _supplier(DELTA_INDUSTRIAL, "Delta Industrial", "201-555-004", "Net 30", stamp, status="inactive"),
    ]
    invoices = [
        # posts fine; the labour line has no account code and gets the default one
        _invoice(
            101,
            "AP-24017",
            ACME_PUMPS,
            today - timedelta(days=3),
            [
                ("Mechanical seal kit, 50 mm", 2, "1850.00", "6100", "WO-1001"),
                ("Labour, pump overhaul", 4, "950.00", None, "WO-1001"),
            ],
            stamp,
        ),
        # dated four months back: that period is closed in the ERP
        _invoice(102, "AP-23988", ACME_PUMPS, today - timedelta(days=120), [_parts("Bearing set")], stamp),
        # AP already keyed this in the ERP by hand, same total: recognised, not posted again
        _invoice(103, "BE-7781", BETA_ELECTRICAL, today - timedelta(days=5), [_panel_work()], stamp),
        # also keyed by hand, but with a different total: flagged for a person
        _invoice(104, "BE-7790", BETA_ELECTRICAL, today - timedelta(days=4), [_panel_work()], stamp),
        # supplier never made it into the ERP
        _invoice(
            105,
            "GOS-0042",
            GAMMA_OFFICE,
            today - timedelta(days=2),
            [("Printer paper, 20 reams", 1, "4200.00", "6400", None)],
            stamp,
        ),
    ]
    names = {s["id"]: s["name"] for s in suppliers}
    for invoice in invoices:
        invoice["supplier"]["name"] = names[invoice["supplier"]["id"]]
    return ProcurementStore(
        suppliers={s["id"]: s for s in suppliers},
        invoices={i["id"]: i for i in invoices},
    )


def erp_store(today: date) -> ErpStore:
    store = ErpStore(company="ACME", fiscal_periods=fiscal_periods(today))
    beta: dict[str, Any] = {
        "vendor_id": "V0001",
        "name": "BETA ELECTRICAL SVCS",
        "tax_id": "201-555-002",
        "active": True,
    }
    store.vendors[beta["vendor_id"]] = beta
    # the two invoices AP keyed in by hand; 1002 was entered with the wrong amount
    store.ap_invoices[1001] = _hand_keyed(1001, "BE-7781", today - timedelta(days=5), Decimal("12500.00"))
    store.ap_invoices[1002] = _hand_keyed(1002, "BE-7790", today - timedelta(days=4), Decimal("11250.00"))
    return store


def fiscal_periods(today: date) -> list[dict[str, Any]]:
    first_open = _first_of_month(_first_of_month(today) - timedelta(days=1))  # start of last month
    periods = []
    for year in (today.year - 1, today.year, today.year + 1):
        for month in range(1, 13):
            start = date(year, month, 1)
            end = _first_of_month(start + timedelta(days=31)) - timedelta(days=1)
            periods.append(
                {
                    "year": year,
                    "period": month,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "closed": end < first_open,
                }
            )
    return periods


def seed_maintenance(db: MaintenanceDatabase, today: date) -> None:
    with db.session() as session:
        session.add_all(
            [
                # set up by hand, not linked to procurement yet
                ContractorRow(code="BETA-EL", name="Beta Electrical", tax_id="201-555-002"),
                WorkOrderRow(
                    number="WO-1001",
                    description="Overhaul cooling water pump P-101",
                    asset_code="P-101",
                    estimated_cost=Decimal("7000.00"),
                    opened_on=today - timedelta(days=10),
                ),
                WorkOrderRow(
                    number="WO-1002",
                    description="Replace distribution board DB-3",
                    status=WorkOrderStatus.IN_PROGRESS,
                    asset_code="DB-3",
                    estimated_cost=Decimal("15000.00"),
                    opened_on=today - timedelta(days=8),
                ),
                WorkOrderRow(
                    number="WO-0990",
                    description="Annual fire pump inspection",
                    status=WorkOrderStatus.COMPLETED,
                    asset_code="FP-1",
                    opened_on=today - timedelta(days=40),
                    closed_on=today - timedelta(days=30),
                ),
            ]
        )


def _supplier(
    supplier_id: int,
    name: str,
    tax_id: str,
    terms: str,
    stamp: str,
    *,
    status: str = "active",
    with_bank: bool = False,
) -> dict[str, Any]:
    supplier: dict[str, Any] = {
        "id": supplier_id,
        "name": name,
        "tax_id": tax_id,
        "status": status,
        "email": f"ap@{name.split()[0].lower()}.example",
        "payment_terms": terms,
        "remit_to": {
            "line1": f"{supplier_id} Industrial Ave",
            "city": "Makati",
            "state": "NCR",
            "country": "PH",
        },
        "exported": False,
        "updated_at": stamp,
    }
    if with_bank:
        supplier["bank_account"] = {
            "account_name": name,
            "bank_name": "Demo Bank",
            "account_number": "000123456",
        }
    return supplier


def _invoice(
    invoice_id: int, number: str, supplier_id: int, day: date, lines: list[Line], stamp: str
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for n, (description, quantity, price, account, work_order) in enumerate(lines, start=1):
        rows.append(
            {
                "line_number": n,
                "description": description,
                "quantity": quantity,
                "unit_price": price,
                "amount": str(quantity * Decimal(price)),
                "account_code": account,
                "work_order_number": work_order,
            }
        )
    subtotal = sum((Decimal(r["amount"]) for r in rows), Decimal("0"))
    tax = (subtotal * VAT).quantize(Decimal("0.01"))
    return {
        "id": invoice_id,
        "invoice_number": number,
        "supplier": {"id": supplier_id, "name": ""},
        "status": "approved",
        "invoice_date": day.isoformat(),
        "due_date": (day + timedelta(days=30)).isoformat(),
        "currency": "PHP",
        "subtotal": str(subtotal),
        "tax_amount": str(tax),
        "total": str(subtotal + tax),
        "lines": rows,
        "exported": False,
        "updated_at": stamp,
    }


def _parts(description: str) -> Line:
    return (description, 1, "2400.00", "6100", "WO-1001")


def _panel_work() -> Line:
    return ("Panel rewiring, DB-3", 1, "12500.00", "6200", "WO-1002")


def _hand_keyed(invoice_id: int, number: str, day: date, subtotal: Decimal) -> dict[str, Any]:
    return {
        "invoice_id": invoice_id,
        "vendor_id": "V0001",
        "invoice_number": number,
        "invoice_date": day.isoformat(),
        "currency": "PHP",
        "lines": [
            {"line_number": 1, "gl_account": "6200", "description": "per invoice", "amount": str(subtotal)}
        ],
        "tax_amount": str((subtotal * VAT).quantize(Decimal("0.01"))),
        "posted": True,
    }


def _first_of_month(day: date) -> date:
    return day.replace(day=1)
