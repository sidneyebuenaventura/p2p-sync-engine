"""Client for the ERP's accounts payable REST API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date
from typing import Any

from pydantic import BaseModel
from requests.auth import HTTPBasicAuth

from p2p_sync.connectors.erp.models import (
    APInvoice,
    APInvoiceDraft,
    FiscalPeriod,
    Vendor,
    VendorDraft,
    VendorPayment,
)
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.http import HttpClient
from p2p_sync.core.settings import ErpSettings


class ClosedPeriodError(P2PSyncError):
    """The invoice date falls in a closed (or missing) fiscal period."""


class ErpClient:
    def __init__(self, http: HttpClient, company: str, *, page_size: int = 100) -> None:
        self.http = http
        self.company = company
        self.page_size = page_size
        self._periods: dict[int, list[FiscalPeriod]] = {}

    @classmethod
    def from_settings(cls, settings: ErpSettings, *, timeout: float = 30.0) -> ErpClient:
        # This ERP wants both: an API key for the integration and a user for the audit trail.
        http = HttpClient(
            settings.base_url,
            auth=HTTPBasicAuth(settings.username, settings.password.get_secret_value()),
            headers={"X-API-Key": settings.api_key.get_secret_value()},
            timeout=timeout,
        )
        return cls(http, settings.company)

    # vendors

    def vendors(self, *, active_only: bool = True) -> Iterator[Vendor]:
        params = {"active": "true"} if active_only else {}
        return self._paginate("vendors", Vendor, params)

    def search_vendors(self, *, tax_id: str | None = None, external_ref: str | None = None) -> list[Vendor]:
        params = {k: v for k, v in {"tax_id": tax_id, "external_ref": external_ref}.items() if v}
        if not params:
            raise ValueError("search_vendors needs tax_id or external_ref")
        return list(self._paginate("vendors", Vendor, params))

    def create_vendor(self, draft: VendorDraft) -> Vendor:
        return Vendor.model_validate(self.http.post_json(self._path("vendors"), _body(draft)))

    def update_vendor(self, vendor_id: str, draft: VendorDraft) -> Vendor:
        return Vendor.model_validate(self.http.put_json(self._path("vendors", vendor_id), _body(draft)))

    # AP invoices

    def ap_invoices(self, *, vendor_id: str | None = None) -> Iterator[APInvoice]:
        params = {"vendor_id": vendor_id} if vendor_id else {}
        return self._paginate("ap-invoices", APInvoice, params)

    def find_ap_invoice(self, vendor_id: str, invoice_number: str) -> APInvoice | None:
        params = {"vendor_id": vendor_id, "invoice_number": invoice_number}
        matches = list(self._paginate("ap-invoices", APInvoice, params))
        if len(matches) > 1:
            raise P2PSyncError(f"{len(matches)} AP invoices for vendor {vendor_id} number {invoice_number}")
        return matches[0] if matches else None

    def create_ap_invoice(self, draft: APInvoiceDraft) -> APInvoice:
        # The ERP would reject it too, but with a vague 400. Checking first gives AP a message
        # they can act on ("period 2026/09 is closed").
        self.ensure_open_period(draft.invoice_date)
        return APInvoice.model_validate(self.http.post_json(self._path("ap-invoices"), _body(draft)))

    def payments(self, *, since: date) -> Iterator[VendorPayment]:
        return self._paginate("payments", VendorPayment, {"paid_since": since.isoformat()})

    # fiscal periods

    def fiscal_periods(self, year: int) -> list[FiscalPeriod]:
        # periods rarely change mid-run, so cache them per client
        if year not in self._periods:
            raw = self.http.get_json(self._path("fiscal-periods"), params={"year": year})
            self._periods[year] = [FiscalPeriod.model_validate(p) for p in raw]
        return self._periods[year]

    def period_for(self, day: date) -> FiscalPeriod | None:
        # a fiscal year can start mid calendar year, so look in both neighbours
        for year in (day.year, day.year + 1, day.year - 1):
            for period in self.fiscal_periods(year):
                if period.contains(day):
                    return period
        return None

    def ensure_open_period(self, day: date) -> FiscalPeriod:
        period = self.period_for(day)
        if period is None:
            raise ClosedPeriodError(f"no fiscal period covers {day.isoformat()}")
        if period.closed:
            raise ClosedPeriodError(f"period {period.year}/{period.period:02d} is closed ({day.isoformat()})")
        return period

    def _path(self, *parts: str) -> str:
        return "/".join(["/api/companies", self.company, *parts])

    def _paginate[M: BaseModel](
        self, resource: str, model: type[M], params: Mapping[str, Any]
    ) -> Iterator[M]:
        page: int | None = 1
        while page is not None:
            body = self.http.get_json(
                self._path(resource), params={**params, "page": page, "page_size": self.page_size}
            )
            for raw in body["items"]:
                yield model.model_validate(raw)
            page = body.get("next_page")


def _body(model: BaseModel) -> Any:
    return model.model_dump(mode="json", exclude_none=True)
