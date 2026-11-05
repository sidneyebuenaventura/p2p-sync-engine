"""Post approved procurement invoices to the ERP as AP invoices.

Insert only: once an invoice is in the ERP, AP owns it there. An invoice is matched
by supplier and invoice number rather than by our own reference, because the thing
to avoid above all is paying the same supplier invoice twice, and AP sometimes keys
an urgent one in by hand before the sync gets to it.
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Iterable

from p2p_sync.connectors.erp import APInvoice, APInvoiceDraft, APInvoiceLine, ErpClient
from p2p_sync.connectors.procurement import IntegrationStatus, Invoice, InvoiceLine, ProcurementClient
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.logging import log_context
from p2p_sync.flows.suppliers import MappingError, supplier_ref
from p2p_sync.sync import RecordMatcher, SyncAction, SyncEngine, SyncReport, SyncResult

logger = logging.getLogger(__name__)

_ERP_DESCRIPTION_MAX = 200


def invoice_ref(invoice_id: int) -> str:
    return f"procurement:invoice:{invoice_id}"


def ap_invoice_draft(
    invoice: Invoice, vendor_id: str, *, default_gl_account: str | None = None
) -> APInvoiceDraft:
    return APInvoiceDraft(
        vendor_id=vendor_id,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        currency=invoice.currency,
        lines=[_ap_line(invoice, line, default_gl_account) for line in invoice.lines],
        tax_amount=invoice.tax_amount,
        external_ref=invoice_ref(invoice.id),
    )


def _ap_line(invoice: Invoice, line: InvoiceLine, default_gl_account: str | None) -> APInvoiceLine:
    gl_account = line.account_code or default_gl_account
    if not gl_account:
        raise MappingError(f"invoice {invoice.invoice_number} line {line.line_number} has no account code")
    return APInvoiceLine(
        line_number=line.line_number,
        gl_account=gl_account,
        description=line.description[:_ERP_DESCRIPTION_MAX],
        amount=line.amount,
        tax_code=line.tax_code,
    )


def _number_key(invoice_number: str) -> str:
    return " ".join(invoice_number.split()).casefold()


class _InvoiceMatcher(RecordMatcher[Invoice, APInvoice]):
    # Both sides are keyed in procurement terms (supplier ref, invoice number), so an
    # invoice whose supplier has no vendor yet simply finds nothing and fails on create.
    def __init__(self, supplier_ref_by_vendor: dict[str, str]) -> None:
        self.supplier_ref_by_vendor = supplier_ref_by_vendor

    def source_key(self, source: Invoice) -> Hashable | None:
        return (supplier_ref(source.supplier.id), _number_key(source.invoice_number))

    def target_key(self, target: APInvoice) -> Hashable | None:
        ref = self.supplier_ref_by_vendor.get(target.vendor_id)
        return (ref, _number_key(target.invoice_number)) if ref else None


class InvoiceSync:
    def __init__(
        self,
        procurement: ProcurementClient,
        erp: ErpClient,
        *,
        default_gl_account: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.procurement = procurement
        self.erp = erp
        self.default_gl_account = default_gl_account
        self.dry_run = dry_run
        self._vendor_by_supplier: dict[str, str] = {}
        self._lookup_errors: dict[int, P2PSyncError] = {}

    def run(self, invoices: Iterable[Invoice] | None = None) -> SyncReport[Invoice, APInvoice]:
        sources = list(invoices) if invoices is not None else list(self.procurement.approved_invoices())
        self._vendor_by_supplier = {
            v.external_ref: v.vendor_id for v in self.erp.vendors(active_only=False) if v.external_ref
        }
        matcher = _InvoiceMatcher({vendor_id: ref for ref, vendor_id in self._vendor_by_supplier.items()})

        engine = SyncEngine(
            matcher,
            create=self._create,
            finalize=self._finalize,
            report_problem=self._report_problem,
            dry_run=self.dry_run,
            logger=logger,
        )
        with log_context(flow="invoices"):
            report = engine.run(sources, self._existing(sources))
            if not self.dry_run:
                for result in report.results:
                    if result.action is SyncAction.SKIP and result.target is not None:
                        self._settle_existing(result.source, result.target)
        return report

    def _existing(self, invoices: list[Invoice]) -> list[APInvoice]:
        # One lookup per invoice. The ERP has years of AP invoices and we only ever have a
        # handful approved and not yet exported, so this beats listing everything.
        found: list[APInvoice] = []
        self._lookup_errors = {}
        for invoice in invoices:
            vendor_id = self._vendor_by_supplier.get(supplier_ref(invoice.supplier.id))
            if vendor_id is None:
                continue
            try:
                existing = self.erp.find_ap_invoice(vendor_id, invoice.invoice_number)
            except P2PSyncError as exc:
                # Not knowing whether it's already there means we mustn't post it.
                self._lookup_errors[invoice.id] = exc
                continue
            if existing is not None:
                found.append(existing)
        return found

    def _create(self, invoice: Invoice) -> APInvoice:
        if invoice.id in self._lookup_errors:
            raise self._lookup_errors[invoice.id]
        vendor_id = self._vendor_by_supplier.get(supplier_ref(invoice.supplier.id))
        if vendor_id is None:
            raise P2PSyncError(
                f"supplier {invoice.supplier.name} ({invoice.supplier.id}) has no ERP vendor yet"
            )
        draft = ap_invoice_draft(invoice, vendor_id, default_gl_account=self.default_gl_account)
        return self.erp.create_ap_invoice(draft)

    def _finalize(self, result: SyncResult[Invoice, APInvoice]) -> None:
        ap_id = result.target.invoice_id if result.target else "?"
        self._mark_done(result.source, f"ERP AP invoice {ap_id}")

    def _report_problem(self, result: SyncResult[Invoice, APInvoice]) -> None:
        problem = str(result.error) if result.error else (result.reason or "conflict")
        self.procurement.set_integration_status(
            "invoices", result.source.id, IntegrationStatus.ERROR, problem
        )

    def _settle_existing(self, invoice: Invoice, existing: APInvoice) -> None:
        try:
            if existing.external_ref == invoice_ref(invoice.id):
                # we posted it on an earlier run but didn't get as far as marking it
                self._mark_done(invoice, f"ERP AP invoice {existing.invoice_id}")
            elif existing.total != invoice.total:
                self.procurement.set_integration_status(
                    "invoices",
                    invoice.id,
                    IntegrationStatus.ERROR,
                    f"already in ERP as AP invoice {existing.invoice_id} with total {existing.total},"
                    f" procurement has {invoice.total}",
                )
            else:
                self._mark_done(
                    invoice, f"already in ERP as AP invoice {existing.invoice_id} (keyed by hand)"
                )
        except Exception:
            logger.exception("could not update procurement invoice %s", invoice.id)

    def _mark_done(self, invoice: Invoice, message: str) -> None:
        self.procurement.mark_invoice_exported(invoice.id)
        self.procurement.set_integration_status("invoices", invoice.id, IntegrationStatus.SUCCESS, message)
