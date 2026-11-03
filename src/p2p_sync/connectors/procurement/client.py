"""Client for the procurement system's REST API."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from p2p_sync.connectors.procurement.auth import ClientCredentialsAuth
from p2p_sync.connectors.procurement.models import (
    IntegrationStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    Supplier,
    SupplierStatus,
)
from p2p_sync.core.http import HttpClient
from p2p_sync.core.settings import ProcurementSettings

logger = logging.getLogger(__name__)

RecordKind = Literal["invoices", "suppliers"]
OnInvalid = Callable[[Mapping[str, Any], ValidationError], None]


def _log_invalid(raw: Mapping[str, Any], error: ValidationError) -> None:
    logger.warning("skipping invalid record id=%s: %s", raw.get("id"), error)


class ProcurementClient:
    def __init__(
        self, http: HttpClient, *, page_size: int = 50, on_invalid: OnInvalid = _log_invalid
    ) -> None:
        self.http = http
        self.page_size = page_size
        # One malformed invoice shouldn't stop the other 200 from syncing, so bad records
        # are handed to on_invalid (which can report them) and skipped.
        self.on_invalid = on_invalid

    @classmethod
    def from_settings(cls, settings: ProcurementSettings, *, timeout: float = 30.0) -> ProcurementClient:
        base_url = settings.base_url.rstrip("/")
        auth = ClientCredentialsAuth(
            f"{base_url}/oauth2/token",
            settings.client_id,
            settings.client_secret.get_secret_value(),
            timeout=timeout,
        )
        return cls(HttpClient(base_url, auth=auth, timeout=timeout), page_size=settings.page_size)

    # invoices

    def approved_invoices(self, *, include_exported: bool = False) -> Iterator[Invoice]:
        params: dict[str, Any] = {"status": InvoiceStatus.APPROVED.value}
        if not include_exported:
            params["exported"] = "false"
        return self._paginate("/api/invoices", Invoice, params)

    def get_invoice(self, invoice_id: int) -> Invoice:
        return Invoice.model_validate(self.http.get_json(f"/api/invoices/{invoice_id}"))

    def mark_invoice_exported(self, invoice_id: int) -> None:
        self.http.patch_json(f"/api/invoices/{invoice_id}", {"exported": True})

    def record_payment(self, invoice_id: int, payment: Payment) -> None:
        self.http.post_json(f"/api/invoices/{invoice_id}/payments", payment.model_dump(mode="json"))

    # suppliers

    def suppliers(
        self, *, status: SupplierStatus = SupplierStatus.ACTIVE, include_exported: bool = False
    ) -> Iterator[Supplier]:
        params: dict[str, Any] = {"status": status.value}
        if not include_exported:
            params["exported"] = "false"
        return self._paginate("/api/suppliers", Supplier, params)

    def get_supplier(self, supplier_id: int) -> Supplier:
        return Supplier.model_validate(self.http.get_json(f"/api/suppliers/{supplier_id}"))

    def mark_supplier_exported(self, supplier_id: int) -> None:
        self.http.patch_json(f"/api/suppliers/{supplier_id}", {"exported": True})

    # both

    def set_integration_status(
        self, kind: RecordKind, record_id: int, status: IntegrationStatus, message: str = ""
    ) -> None:
        """Show the sync result on the record in the procurement UI, so AP staff see why it failed."""
        self.http.put_json(
            f"/api/{kind}/{record_id}/integration-status",
            {"status": status.value, "message": message[:1000]},
        )

    def _paginate[M: BaseModel](self, path: str, model: type[M], params: Mapping[str, Any]) -> Iterator[M]:
        offset = 0
        while True:
            page = self.http.get_json(path, params={**params, "offset": offset, "limit": self.page_size})
            for raw in page:
                try:
                    yield model.model_validate(raw)
                except ValidationError as exc:
                    self.on_invalid(raw, exc)
            if len(page) < self.page_size:
                return
            offset += len(page)
