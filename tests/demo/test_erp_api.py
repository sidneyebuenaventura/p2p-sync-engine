"""The mock ERP API, driven through the real ErpClient."""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from p2p_sync.connectors.erp import APInvoiceDraft, APInvoiceLine, ClosedPeriodError, ErpClient, VendorDraft
from p2p_sync.core.errors import APIError
from p2p_sync.core.http import HttpClient
from p2p_sync.core.settings import ErpSettings
from p2p_sync.demo.erp_api import DEMO_API_KEY, DEMO_PASSWORD, DEMO_USERNAME, ErpStore, create_app
from p2p_sync.demo.server import BackgroundServer


def period(month: int, *, closed: bool) -> dict[str, Any]:
    end = date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1)
    return {
        "year": 2026,
        "period": month,
        "start_date": f"2026-{month:02d}-01",
        "end_date": date.fromordinal(end.toordinal() - 1).isoformat(),
        "closed": closed,
    }


@pytest.fixture
def store() -> ErpStore:
    return ErpStore(
        vendors={"V0001": {"vendor_id": "V0001", "name": "Old Vendor", "tax_id": "111", "active": False}},
        fiscal_periods=[period(m, closed=m < 10) for m in range(1, 13)],
        payments=[
            {
                "payment_id": 1,
                "invoice_id": 1001,
                "vendor_id": "V0002",
                "amount": "50.00",
                "currency": "PHP",
                "paid_on": "2026-11-20",
                "reference": "CHK-1",
            }
        ],
    )


@pytest.fixture
def server(store: ErpStore) -> Iterator[BackgroundServer]:
    with BackgroundServer(create_app(store)) as running:
        yield running


def settings_for(server: BackgroundServer, **overrides: Any) -> ErpSettings:
    values: dict[str, Any] = {
        "base_url": server.url,
        "api_key": SecretStr(DEMO_API_KEY),
        "username": DEMO_USERNAME,
        "password": SecretStr(DEMO_PASSWORD),
    }
    return ErpSettings(**(values | overrides))


@pytest.fixture
def client(server: BackgroundServer) -> ErpClient:
    erp = ErpClient.from_settings(settings_for(server), timeout=5)
    erp.page_size = 1
    return erp


def draft(number: str, day: date = date(2026, 11, 3), vendor_id: str = "V0002") -> APInvoiceDraft:
    return APInvoiceDraft(
        vendor_id=vendor_id,
        invoice_number=number,
        invoice_date=day,
        currency="PHP",
        lines=[APInvoiceLine(line_number=1, gl_account="6100", description="Parts", amount=Decimal("100"))],
        tax_amount=Decimal("12"),
    )


def test_vendor_round_trip_and_paging(client: ErpClient) -> None:
    created = client.create_vendor(VendorDraft(name="Acme Pumps", tax_id="222", external_ref="ref-1"))
    client.update_vendor(created.vendor_id, VendorDraft(name="Acme Pumps Inc", tax_id="222"))

    assert created.vendor_id == "V0002"
    assert [v.name for v in client.vendors(active_only=False)] == ["Old Vendor", "Acme Pumps Inc"]
    assert [v.vendor_id for v in client.vendors()] == ["V0002"]
    assert [v.vendor_id for v in client.search_vendors(tax_id="111")] == ["V0001"]
    assert client.search_vendors(external_ref="ref-1") == []


def test_posts_into_an_open_period_and_finds_it_again(client: ErpClient, server: BackgroundServer) -> None:
    client.create_vendor(VendorDraft(name="Acme Pumps"))

    posted = client.create_ap_invoice(draft("INV-1"))
    found = client.find_ap_invoice("V0002", "INV-1")

    assert posted.posted
    assert found is not None
    assert found.invoice_id == posted.invoice_id == 1001
    status = requests.get(f"{server.url}/demo/status", timeout=5).json()
    assert status["ap_invoices"][0]["total"] == "112"
    assert status["vendors"][1]["name"] == "Acme Pumps"


def test_closed_period_is_caught_before_posting(client: ErpClient, store: ErpStore) -> None:
    client.create_vendor(VendorDraft(name="Acme Pumps"))

    with pytest.raises(ClosedPeriodError, match="2026/09 is closed"):
        client.create_ap_invoice(draft("INV-1", date(2026, 9, 30)))
    assert store.ap_invoices == {}


def test_server_side_rules_without_the_client_checks(server: BackgroundServer, store: ErpStore) -> None:
    # straight HTTP calls, skipping ErpClient's own period check
    settings = settings_for(server)
    http = HttpClient(
        server.url,
        auth=(settings.username, settings.password.get_secret_value()),
        headers={"X-API-Key": DEMO_API_KEY},
        timeout=5,
    )
    path = "/api/companies/ACME/ap-invoices"
    store.vendors["V0002"] = {"vendor_id": "V0002", "name": "Acme Pumps", "active": True}

    def post(body: APInvoiceDraft) -> int:
        try:
            http.post_json(path, body.model_dump(mode="json"))
        except APIError as exc:
            return exc.status_code or 0
        return 201

    assert post(draft("INV-1", date(2026, 9, 30))) == 400  # closed period
    assert post(draft("INV-1", vendor_id="V0001")) == 400  # inactive vendor
    assert post(draft("INV-1")) == 201
    assert post(draft("INV-1")) == 409  # duplicate


def test_payments_since(client: ErpClient) -> None:
    assert [p.reference for p in client.payments(since=date(2026, 11, 1))] == ["CHK-1"]
    assert list(client.payments(since=date(2026, 12, 1))) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": SecretStr("wrong")},
        {"password": SecretStr("wrong")},
        {"company": "GLOBEX"},
    ],
)
def test_rejects_wrong_key_user_or_company(server: BackgroundServer, overrides: dict[str, Any]) -> None:
    client = ErpClient.from_settings(settings_for(server, **overrides), timeout=5)

    with pytest.raises(APIError) as error:
        list(client.vendors())
    assert error.value.status_code in (401, 404)


def test_rejects_non_basic_or_garbled_authorization(server: BackgroundServer) -> None:
    url = f"{server.url}/api/companies/ACME/vendors"
    headers = {"X-API-Key": DEMO_API_KEY}

    assert requests.get(url, headers=headers | {"Authorization": "Bearer x"}, timeout=5).status_code == 401
    assert requests.get(url, headers=headers | {"Authorization": "Basic %%%"}, timeout=5).status_code == 401


def test_updating_unknown_vendor_is_404(client: ErpClient) -> None:
    with pytest.raises(APIError) as error:
        client.update_vendor("V9999", VendorDraft(name="Nobody"))
    assert error.value.status_code == 404
