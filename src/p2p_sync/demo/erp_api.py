"""A stand-in for the ERP's accounts payable REST API.

In memory, plain dicts, only what the integration calls. Like the real thing it wants
an API key and basic auth, scopes everything to a company, and pages results in an
{"items", "next_page"} envelope.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException

DEMO_API_KEY = "demo-erp-key"
DEMO_USERNAME = "svc_p2p"
DEMO_PASSWORD = "demo-only-not-secret"  # noqa: S105 - fixed demo value, not a credential

Record = dict[str, Any]


@dataclass
class ErpStore:
    company: str = "ACME"
    vendors: dict[str, Record] = field(default_factory=dict)
    ap_invoices: dict[int, Record] = field(default_factory=dict)
    fiscal_periods: list[Record] = field(default_factory=list)
    payments: list[Record] = field(default_factory=list)

    def next_vendor_id(self) -> str:
        return f"V{len(self.vendors) + 1:04d}"

    def next_invoice_id(self) -> int:
        return max(self.ap_invoices, default=1000) + 1


def create_app(
    store: ErpStore,
    *,
    api_key: str = DEMO_API_KEY,
    username: str = DEMO_USERNAME,
    password: str = DEMO_PASSWORD,
) -> FastAPI:
    app = FastAPI(title="Mock ERP API")

    def require_auth(
        x_api_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if x_api_key is None or not secrets.compare_digest(x_api_key, api_key):
            raise HTTPException(401, "missing or wrong API key")
        if _basic_credentials(authorization) != (username, password):
            raise HTTPException(401, "missing or wrong user")

    def require_company(company: str) -> None:
        if company != store.company:
            raise HTTPException(404, f"no company {company!r}")

    api = APIRouter(
        prefix="/api/companies/{company}", dependencies=[Depends(require_auth), Depends(require_company)]
    )

    @api.get("/vendors")
    def list_vendors(
        active: bool | None = None,
        tax_id: str | None = None,
        external_ref: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Record:
        found = [
            v
            for v in store.vendors.values()
            if (active is None or v.get("active", True) is active)
            and (tax_id is None or v.get("tax_id") == tax_id)
            and (external_ref is None or v.get("external_ref") == external_ref)
        ]
        return _page(found, page, page_size)

    @api.post("/vendors", status_code=201)
    def create_vendor(body: Annotated[Record, Body()]) -> Record:
        vendor = {**body, "vendor_id": store.next_vendor_id()}
        store.vendors[vendor["vendor_id"]] = vendor
        return vendor

    @api.put("/vendors/{vendor_id}")
    def update_vendor(vendor_id: str, body: Annotated[Record, Body()]) -> Record:
        if vendor_id not in store.vendors:
            raise HTTPException(404, f"vendor {vendor_id} not found")
        store.vendors[vendor_id] = {**body, "vendor_id": vendor_id}
        return store.vendors[vendor_id]

    @api.get("/ap-invoices")
    def list_ap_invoices(
        vendor_id: str | None = None, invoice_number: str | None = None, page: int = 1, page_size: int = 100
    ) -> Record:
        found = [
            i
            for i in store.ap_invoices.values()
            if (vendor_id is None or i["vendor_id"] == vendor_id)
            and (invoice_number is None or i["invoice_number"] == invoice_number)
        ]
        return _page(found, page, page_size)

    @api.post("/ap-invoices", status_code=201)
    def create_ap_invoice(body: Annotated[Record, Body()]) -> Record:
        vendor = store.vendors.get(body.get("vendor_id", ""))
        if vendor is None or not vendor.get("active", True):
            raise HTTPException(400, "vendor not found or inactive")
        if any(
            i["vendor_id"] == body["vendor_id"] and i["invoice_number"] == body["invoice_number"]
            for i in store.ap_invoices.values()
        ):
            raise HTTPException(409, "duplicate invoice number for vendor")
        if not _period_open(store, date.fromisoformat(body["invoice_date"])):
            # real ERPs tend to be this unhelpful, which is why the client checks first
            raise HTTPException(400, "posting not allowed")
        invoice = {**body, "invoice_id": store.next_invoice_id(), "posted": True}
        store.ap_invoices[invoice["invoice_id"]] = invoice
        return invoice

    @api.get("/fiscal-periods")
    def list_fiscal_periods(year: int) -> list[Record]:
        return [p for p in store.fiscal_periods if p["year"] == year]

    @api.get("/payments")
    def list_payments(paid_since: date, page: int = 1, page_size: int = 100) -> Record:
        found = [p for p in store.payments if date.fromisoformat(p["paid_on"]) >= paid_since]
        return _page(found, page, page_size)

    @app.get("/demo/status", tags=["demo"])
    def demo_status() -> Record:
        """Vendors and AP invoices as the ERP has them now."""
        return {
            "vendors": [
                {k: v.get(k) for k in ("vendor_id", "name", "tax_id", "active", "external_ref")}
                for v in store.vendors.values()
            ],
            "ap_invoices": [_invoice_row(i) for i in store.ap_invoices.values()],
        }

    app.include_router(api)
    return app


def _basic_credentials(authorization: str | None) -> tuple[str, str] | None:
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, _, secret = base64.b64decode(encoded, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return user, secret


def _period_open(store: ErpStore, day: date) -> bool:
    return any(
        date.fromisoformat(p["start_date"]) <= day <= date.fromisoformat(p["end_date"]) and not p["closed"]
        for p in store.fiscal_periods
    )


def _page(records: list[Record], page: int, page_size: int) -> Record:
    start = (page - 1) * page_size
    has_more = len(records) > start + page_size
    return {"items": records[start : start + page_size], "next_page": page + 1 if has_more else None}


def _invoice_row(invoice: Record) -> Record:
    subtotal = sum((Decimal(str(line["amount"])) for line in invoice.get("lines", [])), Decimal("0"))
    tax = Decimal(str(invoice.get("tax_amount", "0")))
    return {
        "invoice_id": invoice["invoice_id"],
        "vendor_id": invoice["vendor_id"],
        "invoice_number": invoice["invoice_number"],
        "invoice_date": invoice["invoice_date"],
        "total": str(subtotal + tax),
        "external_ref": invoice.get("external_ref"),
    }
