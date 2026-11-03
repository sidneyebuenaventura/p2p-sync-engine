from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import responses
from pydantic import SecretStr
from responses import matchers

from p2p_sync.connectors.erp import APInvoiceDraft, APInvoiceLine, VendorDraft
from p2p_sync.connectors.erp.client import ClosedPeriodError, ErpClient
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.http import HttpClient
from p2p_sync.core.settings import ErpSettings

BASE = "https://erp.test"
API = f"{BASE}/api/companies/ACME"


def vendor(vendor_id: str, **extra: Any) -> dict[str, Any]:
    return {"vendor_id": vendor_id, "name": f"Vendor {vendor_id}", **extra}


def ap_invoice(invoice_id: int, number: str = "INV-1") -> dict[str, Any]:
    return {
        "invoice_id": invoice_id,
        "vendor_id": "V1",
        "invoice_number": number,
        "invoice_date": "2026-10-01",
        "currency": "PHP",
        "lines": [{"line_number": 1, "gl_account": "6100", "description": "Parts", "amount": "100.00"}],
    }


def period(month: int, closed: bool = False, year: int = 2026) -> dict[str, Any]:
    end_day = 30 if month in (4, 6, 9, 11) else 31
    return {
        "year": year,
        "period": month,
        "start_date": f"{year}-{month:02d}-01",
        "end_date": f"{year}-{month:02d}-{end_day}",
        "closed": closed,
    }


def page(items: list[dict[str, Any]], next_page: int | None = None) -> dict[str, Any]:
    return {"items": items, "next_page": next_page}


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as rsps:
        yield rsps


@pytest.fixture
def client() -> ErpClient:
    return ErpClient(HttpClient(BASE), "ACME", page_size=2)


def draft_invoice(day: date = date(2026, 10, 15)) -> APInvoiceDraft:
    return APInvoiceDraft(
        vendor_id="V1",
        invoice_number="INV-1",
        invoice_date=day,
        currency="PHP",
        lines=[
            APInvoiceLine(line_number=1, gl_account="6100", description="Parts", amount=Decimal("100.00"))
        ],
    )


def test_vendors_follow_next_page(client: ErpClient, mocked: responses.RequestsMock) -> None:
    def q(n: int) -> Any:
        return matchers.query_param_matcher({"active": "true", "page": str(n), "page_size": "2"})

    mocked.get(f"{API}/vendors", json=page([vendor("V1"), vendor("V2")], next_page=2), match=[q(1)])
    mocked.get(f"{API}/vendors", json=page([vendor("V3")]), match=[q(2)])

    assert [v.vendor_id for v in client.vendors()] == ["V1", "V2", "V3"]


def test_search_vendors_by_tax_id(client: ErpClient, mocked: responses.RequestsMock) -> None:
    mocked.get(
        f"{API}/vendors",
        json=page([vendor("V7", tax_id="123")]),
        match=[matchers.query_param_matcher({"tax_id": "123", "page": "1", "page_size": "2"})],
    )

    assert [v.vendor_id for v in client.search_vendors(tax_id="123")] == ["V7"]


def test_search_vendors_needs_a_filter(client: ErpClient) -> None:
    with pytest.raises(ValueError, match="needs tax_id or external_ref"):
        client.search_vendors()


def test_create_and_update_vendor_send_draft_without_nulls(
    client: ErpClient, mocked: responses.RequestsMock
) -> None:
    body = matchers.json_params_matcher({"name": "Acme Pumps", "tax_id": "123", "active": True})
    mocked.post(f"{API}/vendors", json=vendor("V9", tax_id="123"), match=[body])
    mocked.put(f"{API}/vendors/V9", json=vendor("V9", tax_id="123"), match=[body])
    draft = VendorDraft(name="Acme Pumps", tax_id="123")

    assert client.create_vendor(draft).vendor_id == "V9"
    assert client.update_vendor("V9", draft).vendor_id == "V9"


def test_find_ap_invoice(client: ErpClient, mocked: responses.RequestsMock) -> None:
    mocked.get(f"{API}/ap-invoices", json=page([ap_invoice(5)]))
    mocked.get(f"{API}/ap-invoices", json=page([]))

    found = client.find_ap_invoice("V1", "INV-1")
    assert found is not None
    assert found.invoice_id == 5
    assert client.find_ap_invoice("V1", "INV-404") is None


def test_find_ap_invoice_refuses_to_guess_between_duplicates(
    client: ErpClient, mocked: responses.RequestsMock
) -> None:
    mocked.get(f"{API}/ap-invoices", json=page([ap_invoice(5), ap_invoice(6)]))

    with pytest.raises(P2PSyncError, match="2 AP invoices"):
        client.find_ap_invoice("V1", "INV-1")


def test_create_ap_invoice_posts_when_period_is_open(
    client: ErpClient, mocked: responses.RequestsMock
) -> None:
    mocked.get(f"{API}/fiscal-periods", json=[period(9, closed=True), period(10)])
    mocked.post(f"{API}/ap-invoices", json=ap_invoice(77))

    assert client.create_ap_invoice(draft_invoice()).invoice_id == 77
    sent = mocked.calls[-1].request.body
    assert sent is not None
    assert b'"amount":"100.00"' in sent.replace(b" ", b"")


def test_create_ap_invoice_refuses_closed_period(client: ErpClient, mocked: responses.RequestsMock) -> None:
    mocked.get(f"{API}/fiscal-periods", json=[period(9, closed=True), period(10)])

    with pytest.raises(ClosedPeriodError, match=r"period 2026/09 is closed \(2026-09-15\)"):
        client.create_ap_invoice(draft_invoice(date(2026, 9, 15)))

    assert not any(call.request.method == "POST" for call in mocked.calls)


def test_missing_period_is_treated_as_closed(client: ErpClient, mocked: responses.RequestsMock) -> None:
    mocked.get(f"{API}/fiscal-periods", json=[])

    with pytest.raises(ClosedPeriodError, match="no fiscal period covers 2030-01-01"):
        client.ensure_open_period(date(2030, 1, 1))


def test_period_lookup_checks_neighbouring_fiscal_years(
    client: ErpClient, mocked: responses.RequestsMock
) -> None:
    # fiscal year 2027 starts in July 2026
    mocked.get(f"{API}/fiscal-periods", json=[], match=[matchers.query_param_matcher({"year": "2026"})])
    mocked.get(
        f"{API}/fiscal-periods",
        json=[period(7, year=2027) | {"start_date": "2026-07-01", "end_date": "2026-07-31"}],
        match=[matchers.query_param_matcher({"year": "2027"})],
    )

    found = client.period_for(date(2026, 7, 10))
    assert found is not None
    assert found.year == 2027


def test_fiscal_periods_are_cached(client: ErpClient, mocked: responses.RequestsMock) -> None:
    mocked.get(f"{API}/fiscal-periods", json=[period(10)])

    client.fiscal_periods(2026)
    client.fiscal_periods(2026)

    assert len(mocked.calls) == 1


def test_payments_since(client: ErpClient, mocked: responses.RequestsMock) -> None:
    payment = {
        "payment_id": 1,
        "invoice_id": 77,
        "vendor_id": "V1",
        "amount": "100.00",
        "currency": "PHP",
        "paid_on": "2026-10-20",
        "reference": "CHK-1",
    }
    mocked.get(
        f"{API}/payments",
        json=page([payment]),
        match=[matchers.query_param_matcher({"paid_since": "2026-10-01", "page": "1", "page_size": "2"})],
    )

    [paid] = client.payments(since=date(2026, 10, 1))
    assert paid.amount == Decimal("100.00")


def test_from_settings_sends_api_key_and_basic_auth(mocked: responses.RequestsMock) -> None:
    settings = ErpSettings(
        base_url=BASE,
        company="ACME",
        api_key=SecretStr("key-123"),
        username="svc_p2p",
        password=SecretStr("pw"),
    )
    mocked.get(
        f"{API}/vendors",
        json=page([]),
        match=[
            matchers.header_matcher({"X-API-Key": "key-123", "Authorization": "Basic c3ZjX3AycDpwdw=="}),
        ],
    )

    assert list(ErpClient.from_settings(settings).vendors()) == []


def test_ap_invoices_filtered_by_vendor(client: ErpClient, mocked: responses.RequestsMock) -> None:
    mocked.get(
        f"{API}/ap-invoices",
        json=page([ap_invoice(5), ap_invoice(6, "INV-2")]),
        match=[matchers.query_param_matcher({"vendor_id": "V1", "page": "1", "page_size": "2"})],
    )

    assert [inv.invoice_number for inv in client.ap_invoices(vendor_id="V1")] == ["INV-1", "INV-2"]
