from collections.abc import Iterator, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import responses
from pydantic import SecretStr, ValidationError
from responses import matchers

from p2p_sync.connectors.procurement import IntegrationStatus, Payment
from p2p_sync.connectors.procurement.client import ProcurementClient
from p2p_sync.core.http import HttpClient
from p2p_sync.core.settings import ProcurementSettings

BASE = "https://procurement.test"


def invoice(invoice_id: int, number: str | None = None) -> dict[str, Any]:
    return {
        "id": invoice_id,
        "invoice_number": number or f"INV-{invoice_id}",
        "supplier": {"id": 1, "name": "Acme"},
        "status": "approved",
        "invoice_date": "2026-10-01",
        "currency": "PHP",
        "subtotal": "100.00",
        "total": "100.00",
        "lines": [{"line_number": 1, "description": "Parts", "quantity": "1", "unit_price": "100.00",
                   "amount": "100.00"}],
        "updated_at": "2026-10-01T00:00:00Z",
    }  # fmt: skip


def supplier(supplier_id: int) -> dict[str, Any]:
    return {
        "id": supplier_id,
        "name": f"Supplier {supplier_id}",
        "status": "active",
        "updated_at": "2026-10-01T00:00:00Z",
    }


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as rsps:
        yield rsps


@pytest.fixture
def client() -> ProcurementClient:
    return ProcurementClient(HttpClient(BASE), page_size=2)


def page_matcher(offset: int, **extra: str) -> Any:
    return matchers.query_param_matcher({"offset": str(offset), "limit": "2", **extra})


def test_approved_invoices_walks_every_page(
    client: ProcurementClient, mocked: responses.RequestsMock
) -> None:
    filters = {"status": "approved", "exported": "false"}
    mocked.get(f"{BASE}/api/invoices", json=[invoice(1), invoice(2)], match=[page_matcher(0, **filters)])
    mocked.get(f"{BASE}/api/invoices", json=[invoice(3), invoice(4)], match=[page_matcher(2, **filters)])
    mocked.get(f"{BASE}/api/invoices", json=[invoice(5)], match=[page_matcher(4, **filters)])

    assert [inv.id for inv in client.approved_invoices()] == [1, 2, 3, 4, 5]


def test_stops_on_an_empty_page(client: ProcurementClient, mocked: responses.RequestsMock) -> None:
    mocked.get(
        f"{BASE}/api/invoices", json=[invoice(1), invoice(2)], match=[page_matcher(0, status="approved")]
    )
    mocked.get(f"{BASE}/api/invoices", json=[], match=[page_matcher(2, status="approved")])

    assert len(list(client.approved_invoices(include_exported=True))) == 2


def test_invalid_records_are_reported_and_skipped(mocked: responses.RequestsMock) -> None:
    broken = invoice(2)
    broken["total"] = "999.00"
    mocked.get(f"{BASE}/api/invoices", json=[invoice(1), broken, invoice(3)])
    rejected: list[tuple[Mapping[str, Any], ValidationError]] = []
    client = ProcurementClient(HttpClient(BASE), on_invalid=lambda raw, err: rejected.append((raw, err)))

    assert [inv.id for inv in client.approved_invoices()] == [1, 3]
    assert [raw["id"] for raw, _ in rejected] == [2]


def test_invalid_records_are_logged_by_default(
    client: ProcurementClient, mocked: responses.RequestsMock, caplog: pytest.LogCaptureFixture
) -> None:
    broken = invoice(9)
    broken["lines"] = []
    mocked.get(f"{BASE}/api/invoices", json=[broken])

    assert list(client.approved_invoices()) == []
    assert "skipping invalid record id=9" in caplog.text


def test_suppliers(client: ProcurementClient, mocked: responses.RequestsMock) -> None:
    mocked.get(
        f"{BASE}/api/suppliers",
        json=[supplier(7)],
        match=[page_matcher(0, status="active", exported="false")],
    )

    assert [s.name for s in client.suppliers()] == ["Supplier 7"]


def test_get_single_records(client: ProcurementClient, mocked: responses.RequestsMock) -> None:
    mocked.get(f"{BASE}/api/invoices/5", json=invoice(5))
    mocked.get(f"{BASE}/api/suppliers/7", json=supplier(7))

    assert client.get_invoice(5).invoice_number == "INV-5"
    assert client.get_supplier(7).id == 7


def test_mark_exported(client: ProcurementClient, mocked: responses.RequestsMock) -> None:
    body = matchers.json_params_matcher({"exported": True})
    mocked.patch(f"{BASE}/api/invoices/5", json={}, match=[body])
    mocked.patch(f"{BASE}/api/suppliers/7", json={}, match=[body])

    client.mark_invoice_exported(5)
    client.mark_supplier_exported(7)


def test_record_payment_sends_json_safe_values(
    client: ProcurementClient, mocked: responses.RequestsMock
) -> None:
    mocked.post(
        f"{BASE}/api/invoices/5/payments",
        json={},
        match=[
            matchers.json_params_matcher(
                {"amount": "100.00", "paid_on": "2026-10-20", "reference": "CHK-1", "currency": "PHP"}
            )
        ],
    )

    client.record_payment(
        5, Payment(amount=Decimal("100.00"), paid_on=date(2026, 10, 20), reference="CHK-1", currency="PHP")
    )


def test_set_integration_status_truncates_long_messages(
    client: ProcurementClient, mocked: responses.RequestsMock
) -> None:
    mocked.put(
        f"{BASE}/api/invoices/5/integration-status",
        json={},
        match=[matchers.json_params_matcher({"status": "error", "message": "x" * 1000})],
    )

    client.set_integration_status("invoices", 5, IntegrationStatus.ERROR, "x" * 5000)


def test_from_settings_wires_oauth(mocked: responses.RequestsMock) -> None:
    settings = ProcurementSettings(
        base_url=BASE + "/", client_id="id", client_secret=SecretStr("s"), page_size=10
    )
    mocked.post(f"{BASE}/oauth2/token", json={"access_token": "tok", "expires_in": 3600})
    mocked.get(
        f"{BASE}/api/suppliers/7",
        json=supplier(7),
        match=[matchers.header_matcher({"Authorization": "Bearer tok"})],
    )

    client = ProcurementClient.from_settings(settings)

    assert client.page_size == 10
    assert client.get_supplier(7).id == 7
