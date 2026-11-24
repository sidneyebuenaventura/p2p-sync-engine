"""The mock procurement API, driven through the real ProcurementClient."""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from p2p_sync.connectors.procurement import IntegrationStatus, Payment, ProcurementClient
from p2p_sync.core.errors import APIError, AuthenticationError
from p2p_sync.core.settings import ProcurementSettings
from p2p_sync.demo.procurement_api import DEMO_CLIENT_ID, DEMO_CLIENT_SECRET, ProcurementStore, create_app
from p2p_sync.demo.server import BackgroundServer


def supplier(supplier_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": supplier_id,
        "name": f"Supplier {supplier_id}",
        "status": "active",
        "exported": False,
        "updated_at": "2026-11-01T00:00:00Z",
        **extra,
    }


def invoice(invoice_id: int) -> dict[str, Any]:
    return {
        "id": invoice_id,
        "invoice_number": f"INV-{invoice_id}",
        "supplier": {"id": 1, "name": "Supplier 1"},
        "status": "approved",
        "invoice_date": "2026-11-01",
        "currency": "PHP",
        "subtotal": "100.00",
        "total": "100.00",
        "lines": [
            {"line_number": 1, "description": "x", "quantity": 1, "unit_price": "100", "amount": "100"}
        ],
        "exported": False,
        "updated_at": "2026-11-01T00:00:00Z",
    }


@pytest.fixture
def store() -> ProcurementStore:
    return ProcurementStore(
        suppliers={i: supplier(i) for i in range(1, 6)} | {6: supplier(6, status="inactive")},
        invoices={1: invoice(1)},
    )


@pytest.fixture
def server(store: ProcurementStore) -> Iterator[BackgroundServer]:
    with BackgroundServer(create_app(store)) as running:
        yield running


@pytest.fixture
def client(server: BackgroundServer) -> ProcurementClient:
    settings = ProcurementSettings(
        base_url=server.url, client_id=DEMO_CLIENT_ID, client_secret=DEMO_CLIENT_SECRET, page_size=2
    )
    return ProcurementClient.from_settings(settings, timeout=5)


def test_lists_and_pages_suppliers(client: ProcurementClient) -> None:
    assert [s.id for s in client.suppliers()] == [1, 2, 3, 4, 5]


def test_exported_flag_filters_and_can_be_set(client: ProcurementClient, store: ProcurementStore) -> None:
    client.mark_supplier_exported(2)
    client.mark_invoice_exported(1)

    assert [s.id for s in client.suppliers()] == [1, 3, 4, 5]
    assert len(list(client.suppliers(include_exported=True))) == 5
    assert store.invoices[1]["exported"] is True
    assert list(client.approved_invoices()) == []


def test_integration_status_shows_on_demo_page(client: ProcurementClient, server: BackgroundServer) -> None:
    client.set_integration_status("invoices", 1, IntegrationStatus.ERROR, "period 2026/09 is closed")

    status = requests.get(f"{server.url}/demo/status", timeout=5).json()

    assert status["invoices"] == [
        {
            "id": 1,
            "record": "INV-1",
            "exported": False,
            "sync_status": "error",
            "message": "period 2026/09 is closed",
        }
    ]
    assert status["suppliers"][0]["sync_status"] is None


def test_records_payments_and_reads_single_records(
    client: ProcurementClient, store: ProcurementStore
) -> None:
    client.record_payment(
        1, Payment(amount=Decimal("100.00"), paid_on="2026-11-20", reference="CHK-1", currency="PHP")
    )

    assert store.payments[1][0]["reference"] == "CHK-1"
    assert client.get_invoice(1).invoice_number == "INV-1"
    assert client.get_supplier(3).name == "Supplier 3"


def test_unknown_records_are_404(client: ProcurementClient) -> None:
    with pytest.raises(APIError) as error:
        client.get_supplier(99)
    assert error.value.status_code == 404

    with pytest.raises(APIError, match="404"):
        client.set_integration_status("widgets", 1, IntegrationStatus.SUCCESS)


def test_only_the_exported_flag_is_editable(client: ProcurementClient) -> None:
    with pytest.raises(APIError) as error:
        client.http.patch_json("/api/suppliers/1", {"name": "Renamed"})

    assert error.value.status_code == 422


def test_wrong_credentials_are_rejected(server: BackgroundServer) -> None:
    settings = ProcurementSettings(
        base_url=server.url, client_id=DEMO_CLIENT_ID, client_secret=SecretStr("wrong")
    )

    with pytest.raises(AuthenticationError, match="rejected"):
        list(ProcurementClient.from_settings(settings, timeout=5).suppliers())


def test_api_needs_a_token_and_a_supported_grant(server: BackgroundServer) -> None:
    assert requests.get(f"{server.url}/api/suppliers", timeout=5).status_code == 401
    assert (
        requests.post(f"{server.url}/oauth2/token", data={"grant_type": "password"}, timeout=5).status_code
        == 400
    )
