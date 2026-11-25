"""A stand-in for the procurement system's REST API.

Everything lives in memory and only what the integration calls is implemented. It is
written against plain dicts rather than the integration's models on purpose, so a
mistake in those models can't hide behind the same mistake here.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Request

DEMO_CLIENT_ID = "acme-integration"
DEMO_CLIENT_SECRET = "demo-only-not-secret"  # noqa: S105 - fixed demo value, not a credential

Record = dict[str, Any]


@dataclass
class ProcurementStore:
    suppliers: dict[int, Record] = field(default_factory=dict)
    invoices: dict[int, Record] = field(default_factory=dict)
    payments: dict[int, list[Record]] = field(default_factory=dict)
    tokens: set[str] = field(default_factory=set)


def create_app(
    store: ProcurementStore,
    *,
    client_id: str = DEMO_CLIENT_ID,
    client_secret: str = DEMO_CLIENT_SECRET,
) -> FastAPI:
    app = FastAPI(title="Mock procurement API")

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if (authorization or "").removeprefix("Bearer ") not in store.tokens:
            raise HTTPException(401, "invalid or missing bearer token")

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @app.post("/oauth2/token")
    async def token(request: Request) -> Record:
        # parsed by hand so the mock doesn't need python-multipart just for this
        form = {key: values[0] for key, values in parse_qs((await request.body()).decode()).items()}
        if form.get("grant_type") != "client_credentials":
            raise HTTPException(400, "unsupported grant_type")
        if (form.get("client_id"), form.get("client_secret")) != (client_id, client_secret):
            raise HTTPException(401, "bad client credentials")
        issued = secrets.token_urlsafe(24)
        store.tokens.add(issued)
        return {"access_token": issued, "token_type": "bearer", "expires_in": 3600}

    @api.get("/suppliers")
    def list_suppliers(
        status: str | None = None, exported: bool | None = None, offset: int = 0, limit: int = 50
    ) -> list[Record]:
        return _page(_filtered(store.suppliers.values(), status, exported), offset, limit)

    @api.get("/suppliers/{supplier_id}")
    def get_supplier(supplier_id: int) -> Record:
        return _get(store.suppliers, supplier_id, "supplier")

    @api.patch("/suppliers/{supplier_id}")
    def patch_supplier(supplier_id: int, changes: Annotated[Record, Body()]) -> Record:
        return _patch(_get(store.suppliers, supplier_id, "supplier"), changes)

    @api.get("/invoices")
    def list_invoices(
        status: str | None = None, exported: bool | None = None, offset: int = 0, limit: int = 50
    ) -> list[Record]:
        return _page(_filtered(store.invoices.values(), status, exported), offset, limit)

    @api.get("/invoices/{invoice_id}")
    def get_invoice(invoice_id: int) -> Record:
        return _get(store.invoices, invoice_id, "invoice")

    @api.patch("/invoices/{invoice_id}")
    def patch_invoice(invoice_id: int, changes: Annotated[Record, Body()]) -> Record:
        return _patch(_get(store.invoices, invoice_id, "invoice"), changes)

    @api.post("/invoices/{invoice_id}/payments", status_code=201)
    def add_payment(invoice_id: int, payment: Annotated[Record, Body()]) -> Record:
        _get(store.invoices, invoice_id, "invoice")
        store.payments.setdefault(invoice_id, []).append(payment)
        return payment

    @api.put("/{kind}/{record_id}/integration-status")
    def set_integration_status(kind: str, record_id: int, body: Annotated[Record, Body()]) -> Record:
        records = {"suppliers": store.suppliers, "invoices": store.invoices}.get(kind)
        if records is None:
            raise HTTPException(404, f"unknown record type {kind!r}")
        record = _get(records, record_id, kind.rstrip("s"))
        record["integration_status"] = {
            "status": body.get("status"),
            "message": body.get("message", ""),
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        return record

    @app.get("/demo/status", tags=["demo"])
    def demo_status() -> Record:
        """What AP would see in the procurement UI: each record and its sync result."""
        return {
            "suppliers": [_status_row(s, s["name"]) for s in store.suppliers.values()],
            "invoices": [_status_row(i, i["invoice_number"]) for i in store.invoices.values()],
        }

    app.include_router(api)
    return app


def _filtered(records: Iterable[Record], status: str | None, exported: bool | None) -> list[Record]:
    return [
        r
        for r in records
        if (status is None or r["status"] == status) and (exported is None or r.get("exported") is exported)
    ]


def _page(records: list[Record], offset: int, limit: int) -> list[Record]:
    return records[offset : offset + limit]


def _get(records: dict[int, Record], record_id: int, what: str) -> Record:
    if record_id not in records:
        raise HTTPException(404, f"{what} {record_id} not found")
    return records[record_id]


def _patch(record: Record, changes: Record) -> Record:
    # the integration may only flip the exported flag; everything else is owned by users
    unknown = set(changes) - {"exported"}
    if unknown:
        raise HTTPException(422, f"fields not editable via the API: {sorted(unknown)}")
    record.update(changes)
    return record


def _status_row(record: Record, label: str) -> Record:
    status = record.get("integration_status") or {}
    return {
        "id": record["id"],
        "record": label,
        "exported": record.get("exported", False),
        "sync_status": status.get("status"),
        "message": status.get("message"),
    }
