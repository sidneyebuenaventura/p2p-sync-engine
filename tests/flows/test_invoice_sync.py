import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from p2p_sync.connectors.erp import APInvoice, Vendor
from p2p_sync.connectors.procurement import IntegrationStatus, Invoice
from p2p_sync.flows import InvoiceSync, MappingError, ap_invoice_draft
from p2p_sync.sync import SyncAction

MakeInvoice = Callable[..., Invoice]


@pytest.fixture
def erp_with_vendor(erp: Any) -> Any:
    erp.vendor_list = [Vendor(vendor_id="V001", name="Supplier 1", external_ref="procurement:supplier:1")]
    return erp


@pytest.fixture
def sync(procurement: Any, erp_with_vendor: Any) -> InvoiceSync:
    return InvoiceSync(procurement, erp_with_vendor, default_gl_account="6000")


def hand_keyed(number: str, total: str = "336.00", external_ref: str | None = None) -> APInvoice:
    subtotal = Decimal(total) - Decimal("36.00")
    return APInvoice(
        invoice_id=42,
        vendor_id="V001",
        invoice_number=number,
        invoice_date=date(2026, 10, 15),
        currency="PHP",
        lines=[{"line_number": 1, "gl_account": "6000", "description": "per invoice", "amount": subtotal}],
        tax_amount=Decimal("36.00"),
        external_ref=external_ref,
    )


def test_draft_uses_default_account_and_trims_descriptions(make_invoice: MakeInvoice) -> None:
    invoice = make_invoice(
        1,
        lines=[
            {"line_number": 1, "description": "x" * 250, "quantity": 1, "unit_price": "300", "amount": "300"},
        ],
    )

    draft = ap_invoice_draft(invoice, "V001", default_gl_account="6000")

    assert draft.lines[0].gl_account == "6000"
    assert len(draft.lines[0].description) == 200
    assert draft.total == invoice.total
    assert draft.external_ref == "procurement:invoice:1"


def test_draft_needs_an_account_code(make_invoice: MakeInvoice) -> None:
    with pytest.raises(MappingError, match="INV-1 line 2 has no account code"):
        ap_invoice_draft(make_invoice(1), "V001")


def test_posts_invoice_and_marks_it_exported(
    sync: InvoiceSync, procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice
) -> None:
    procurement.invoice_list = [make_invoice(1)]

    report = sync.run()

    assert report.counts()["create"] == 1
    [posted] = erp_with_vendor.posted
    assert (posted.vendor_id, posted.invoice_number, posted.total) == ("V001", "INV-1", Decimal("336.00"))
    assert procurement.exported_invoices == [1]
    assert procurement.statuses[("invoices", 1)] == (IntegrationStatus.SUCCESS, "ERP AP invoice 500")


def test_supplier_without_vendor_fails_with_a_clear_message(
    sync: InvoiceSync, procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice
) -> None:
    report = sync.run([make_invoice(1, supplier_id=9)])

    assert len(report.failures) == 1
    assert erp_with_vendor.posted == []
    status, message = procurement.statuses[("invoices", 1)]
    assert status is IntegrationStatus.ERROR
    assert message == "supplier Supplier 9 (9) has no ERP vendor yet"


def test_closed_period_is_shown_on_the_invoice(
    sync: InvoiceSync, procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice
) -> None:
    erp_with_vendor.closed_before = date(2026, 10, 1)

    report = sync.run([make_invoice(1, invoice_date=date(2026, 9, 28)), make_invoice(2)])

    assert [r.action for r in report.results if r.error is None] == [SyncAction.CREATE]
    assert procurement.statuses[("invoices", 1)] == (
        IntegrationStatus.ERROR,
        "period 2026/09 is closed (2026-09-28)",
    )
    assert procurement.exported_invoices == [2]


def test_rerun_after_a_half_finished_post_just_marks_it(
    sync: InvoiceSync, procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice
) -> None:
    erp_with_vendor.ap_invoice_list = [hand_keyed("INV-1", external_ref="procurement:invoice:1")]

    report = sync.run([make_invoice(1)])

    assert report.results[0].action is SyncAction.SKIP
    assert erp_with_vendor.posted == []
    assert procurement.statuses[("invoices", 1)] == (IntegrationStatus.SUCCESS, "ERP AP invoice 42")


def test_hand_keyed_invoice_with_same_total_is_not_posted_again(
    sync: InvoiceSync, procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice
) -> None:
    erp_with_vendor.ap_invoice_list = [hand_keyed("INV-1")]

    sync.run([make_invoice(1)])

    assert erp_with_vendor.posted == []
    assert procurement.exported_invoices == [1]
    assert procurement.statuses[("invoices", 1)][1] == "already in ERP as AP invoice 42 (keyed by hand)"


def test_hand_keyed_invoice_with_different_total_needs_a_person(
    sync: InvoiceSync, procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice
) -> None:
    erp_with_vendor.ap_invoice_list = [hand_keyed("INV-1", total="400.00")]

    sync.run([make_invoice(1)])

    assert erp_with_vendor.posted == []
    assert procurement.exported_invoices == []
    status, message = procurement.statuses[("invoices", 1)]
    assert status is IntegrationStatus.ERROR
    assert message == "already in ERP as AP invoice 42 with total 400.00, procurement has 336.00"


def test_duplicate_lookup_means_do_not_post(
    sync: InvoiceSync, procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice
) -> None:
    erp_with_vendor.ap_invoice_list = [hand_keyed("INV-1"), hand_keyed("INV-1")]

    report = sync.run([make_invoice(1)])

    assert erp_with_vendor.posted == []
    assert "2 AP invoices for vendor V001" in str(report.failures[0].error)


def test_problem_updating_procurement_after_skip_is_logged_not_raised(
    sync: InvoiceSync,
    procurement: Any,
    erp_with_vendor: Any,
    make_invoice: MakeInvoice,
    caplog: pytest.LogCaptureFixture,
) -> None:
    erp_with_vendor.ap_invoice_list = [hand_keyed("INV-1")]

    def unavailable(invoice_id: int) -> None:
        raise ConnectionError("procurement is down")

    procurement.mark_invoice_exported = unavailable

    with caplog.at_level(logging.ERROR):
        sync.run([make_invoice(1)])

    assert "could not update procurement invoice 1" in caplog.text


def test_dry_run_posts_nothing(procurement: Any, erp_with_vendor: Any, make_invoice: MakeInvoice) -> None:
    erp_with_vendor.ap_invoice_list = [hand_keyed("INV-2")]
    sync = InvoiceSync(procurement, erp_with_vendor, default_gl_account="6000", dry_run=True)

    report = sync.run([make_invoice(1), make_invoice(2)])

    assert report.counts()["create"] == 1
    assert report.counts()["skip"] == 1
    assert erp_with_vendor.posted == []
    assert procurement.statuses == {}
