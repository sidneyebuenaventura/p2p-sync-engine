"""Push procurement suppliers out to the other systems.

Procurement is where suppliers get onboarded and approved, so it owns their details.
Copies are linked back by the procurement supplier id: external_ref on an ERP vendor,
procurement_supplier_id on a maintenance contractor.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping

from pydantic import ValidationError

from p2p_sync.connectors.erp import ErpAddress, ErpClient, Vendor, VendorBank, VendorDraft
from p2p_sync.connectors.maintenance import Contractor, ContractorDraft, MaintenanceClient
from p2p_sync.connectors.procurement import (
    Address,
    BankAccount,
    IntegrationStatus,
    ProcurementClient,
    Supplier,
    SupplierStatus,
)
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.logging import log_context
from p2p_sync.sync import KeyMatcher, SyncEngine, SyncReport, SyncResult

logger = logging.getLogger(__name__)

# procurement payment terms (case and spacing ignored) -> ERP terms code
PAYMENT_TERMS: Mapping[str, str] = {
    "due on receipt": "COD",
    "net 7": "N07",
    "net 14": "N14",
    "net 30": "N30",
    "net 45": "N45",
    "net 60": "N60",
}


class MappingError(P2PSyncError):
    """A value from the source system has no equivalent in the target system."""


def supplier_ref(supplier_id: int) -> str:
    return f"procurement:supplier:{supplier_id}"


def suppliers_to_sync(procurement: ProcurementClient) -> list[Supplier]:
    active = procurement.suppliers(include_exported=True)
    # Inactive suppliers only matter if we sent them out before: their copies need switching off.
    inactive = procurement.suppliers(status=SupplierStatus.INACTIVE, include_exported=True)
    return [*active, *(s for s in inactive if s.exported)]


def vendor_draft(supplier: Supplier, payment_terms: Mapping[str, str] = PAYMENT_TERMS) -> VendorDraft:
    return VendorDraft(
        name=supplier.name,
        tax_id=supplier.tax_id,
        email=supplier.email,
        payment_terms_code=_terms_code(supplier.payment_terms, payment_terms),
        address=_erp_address(supplier.remit_to),
        bank=_vendor_bank(supplier.bank_account),
        active=supplier.status is SupplierStatus.ACTIVE,
        external_ref=supplier_ref(supplier.id),
    )


def contractor_code(supplier: Supplier) -> str:
    return f"PS{supplier.id:06d}"


def contractor_draft(supplier: Supplier, *, code: str) -> ContractorDraft:
    return ContractorDraft(
        code=code,
        name=supplier.name,
        tax_id=supplier.tax_id,
        email=supplier.email,
        active=supplier.status is SupplierStatus.ACTIVE,
        procurement_supplier_id=supplier.id,
    )


def _terms_code(terms: str | None, mapping: Mapping[str, str]) -> str | None:
    if terms is None:
        return None
    code = mapping.get(" ".join(terms.split()).casefold())
    if code is None:
        # guessing a default here would quietly change when the supplier gets paid
        raise MappingError(f"no ERP payment terms code for {terms!r}")
    return code


def _erp_address(address: Address | None) -> ErpAddress | None:
    if address is None:
        return None
    return ErpAddress(
        line1=address.line1,
        line2=address.line2,
        city=address.city,
        region=address.state,
        postal_code=address.postal_code,
        country=address.country,
    )


def _vendor_bank(bank: BankAccount | None) -> VendorBank | None:
    return VendorBank.model_validate(bank.model_dump()) if bank else None


def _as_draft(vendor: Vendor) -> VendorDraft:
    return VendorDraft.model_validate(vendor.model_dump(include=set(VendorDraft.model_fields)))


def _problem[T](result: SyncResult[Supplier, T]) -> str:
    return str(result.error) if result.error else (result.reason or "conflict")


class SupplierVendorSync:
    def __init__(
        self,
        procurement: ProcurementClient,
        erp: ErpClient,
        *,
        payment_terms: Mapping[str, str] = PAYMENT_TERMS,
        dry_run: bool = False,
    ) -> None:
        self.procurement = procurement
        self.erp = erp
        self.payment_terms = payment_terms
        self.dry_run = dry_run

    def run(self, suppliers: Iterable[Supplier] | None = None) -> SyncReport[Supplier, Vendor]:
        sources = list(suppliers) if suppliers is not None else suppliers_to_sync(self.procurement)
        engine = SyncEngine(
            KeyMatcher[Supplier, Vendor](lambda s: supplier_ref(s.id), lambda v: v.external_ref),
            create=self._create,
            update=self._update,
            needs_update=self._needs_update,
            finalize=self._finalize,
            report_problem=self._report_problem,
            dry_run=self.dry_run,
            logger=logger,
        )
        with log_context(flow="supplier-vendor"):
            return engine.run(sources, self.erp.vendors(active_only=False))

    def _create(self, supplier: Supplier) -> Vendor:
        draft = vendor_draft(supplier, self.payment_terms)
        # Vendors keyed in by hand before the integration existed have no external_ref.
        # Take over the one with the same tax id rather than make a duplicate AP has to merge.
        if supplier.tax_id:
            unlinked = [v for v in self.erp.search_vendors(tax_id=supplier.tax_id) if not v.external_ref]
            if len(unlinked) > 1:
                raise P2PSyncError(f"{len(unlinked)} unlinked ERP vendors have tax id {supplier.tax_id}")
            if unlinked:
                logger.info("linking supplier %s to existing vendor %s", supplier.id, unlinked[0].vendor_id)
                return self.erp.update_vendor(unlinked[0].vendor_id, draft)
        return self.erp.create_vendor(draft)

    def _update(self, supplier: Supplier, vendor: Vendor) -> Vendor:
        return self.erp.update_vendor(vendor.vendor_id, vendor_draft(supplier, self.payment_terms))

    def _needs_update(self, supplier: Supplier, vendor: Vendor) -> bool:
        try:
            return vendor_draft(supplier, self.payment_terms) != _as_draft(vendor)
        except (MappingError, ValidationError):
            # say yes, so the update runs, fails again and gets recorded against this supplier
            return True

    def _finalize(self, result: SyncResult[Supplier, Vendor]) -> None:
        supplier = result.source
        if not supplier.exported:
            self.procurement.mark_supplier_exported(supplier.id)
        vendor_id = result.target.vendor_id if result.target else "?"
        self.procurement.set_integration_status(
            "suppliers", supplier.id, IntegrationStatus.SUCCESS, f"ERP vendor {vendor_id}"
        )

    def _report_problem(self, result: SyncResult[Supplier, Vendor]) -> None:
        self.procurement.set_integration_status(
            "suppliers", result.source.id, IntegrationStatus.ERROR, f"ERP vendor sync: {_problem(result)}"
        )


class SupplierContractorSync:
    # Problems here only go to the log and the report. The integration status on the
    # procurement supplier belongs to the ERP sync, which is the one AP keeps an eye on.

    def __init__(
        self,
        procurement: ProcurementClient,
        maintenance: MaintenanceClient,
        *,
        wanted: Callable[[Supplier], bool] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.procurement = procurement
        self.maintenance = maintenance
        # Not every supplier works on site; e.g. wanted=lambda s: s.id in field_service_ids
        self.wanted = wanted
        self.dry_run = dry_run
        self._unlinked: dict[str, list[Contractor]] = {}

    def run(self, suppliers: Iterable[Supplier] | None = None) -> SyncReport[Supplier, Contractor]:
        contractors = self.maintenance.contractors(active_only=False)
        linked = {c.procurement_supplier_id for c in contractors}
        candidates = list(suppliers) if suppliers is not None else suppliers_to_sync(self.procurement)
        sources = [s for s in candidates if self._in_scope(s, linked)]

        unlinked: defaultdict[str, list[Contractor]] = defaultdict(list)
        for contractor in contractors:
            if contractor.procurement_supplier_id is None and contractor.tax_id:
                unlinked[contractor.tax_id].append(contractor)
        self._unlinked = unlinked

        engine = SyncEngine(
            KeyMatcher[Supplier, Contractor](lambda s: s.id, lambda c: c.procurement_supplier_id),
            create=self._create,
            update=self._update,
            needs_update=self._needs_update,
            dry_run=self.dry_run,
            logger=logger,
        )
        with log_context(flow="supplier-contractor"):
            return engine.run(sources, contractors)

    def _in_scope(self, supplier: Supplier, linked: set[int | None]) -> bool:
        if self.wanted is not None and not self.wanted(supplier):
            return False
        # an inactive supplier only matters if it has a contractor to switch off
        return supplier.status is SupplierStatus.ACTIVE or supplier.id in linked

    def _create(self, supplier: Supplier) -> Contractor:
        # same idea as the ERP: adopt a contractor set up by hand with the same tax id
        unlinked = self._unlinked.get(supplier.tax_id or "", [])
        if len(unlinked) > 1:
            raise P2PSyncError(f"{len(unlinked)} unlinked contractors have tax id {supplier.tax_id}")
        if unlinked:
            existing = unlinked.pop()
            logger.info("linking supplier %s to existing contractor %s", supplier.id, existing.code)
            return self.maintenance.update_contractor(
                existing.id, contractor_draft(supplier, code=existing.code)
            )
        return self.maintenance.create_contractor(contractor_draft(supplier, code=contractor_code(supplier)))

    def _update(self, supplier: Supplier, contractor: Contractor) -> Contractor:
        # keep the contractor's code; people on the maintenance side know them by it
        draft = contractor_draft(supplier, code=contractor.code)
        return self.maintenance.update_contractor(contractor.id, draft)

    def _needs_update(self, supplier: Supplier, contractor: Contractor) -> bool:
        current = ContractorDraft.model_validate(
            contractor.model_dump(include=set(ContractorDraft.model_fields))
        )
        return contractor_draft(supplier, code=contractor.code) != current
