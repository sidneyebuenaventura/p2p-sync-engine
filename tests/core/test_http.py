from collections.abc import Iterator

import pytest
import requests
import responses
from requests.auth import HTTPBasicAuth
from responses import matchers

from p2p_sync.core.errors import APIError, AuthenticationError, NotFoundError
from p2p_sync.core.http import RETRY_STATUS_CODES, HttpClient

BASE = "https://api.example.com/v1"


@pytest.fixture
def client() -> Iterator[HttpClient]:
    with HttpClient(BASE + "/", timeout=5) as http:
        yield http


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as rsps:
        yield rsps


def test_url_for_joins_and_passes_absolute_urls_through(client: HttpClient) -> None:
    assert client.url_for("/invoices") == f"{BASE}/invoices"
    assert client.url_for("invoices") == f"{BASE}/invoices"
    assert client.url_for("https://other.example.com/page/2") == "https://other.example.com/page/2"


def test_get_json_sends_params_and_default_headers(
    client: HttpClient, mocked: responses.RequestsMock
) -> None:
    mocked.get(
        f"{BASE}/invoices",
        json=[{"id": 1}],
        match=[
            matchers.query_param_matcher({"status": "approved"}),
            matchers.header_matcher({"Accept": "application/json"}),
        ],
    )

    assert client.get_json("/invoices", params={"status": "approved"}) == [{"id": 1}]
    assert mocked.calls[0].request.headers["User-Agent"].startswith("p2p-sync-engine/")


def test_post_json_sends_body(client: HttpClient, mocked: responses.RequestsMock) -> None:
    mocked.post(
        f"{BASE}/invoices",
        json={"id": 7},
        status=201,
        match=[matchers.json_params_matcher({"amount": "10.00"})],
    )

    assert client.post_json("/invoices", {"amount": "10.00"}) == {"id": 7}


@pytest.mark.parametrize("method", ["put_json", "patch_json"])
def test_put_and_patch_return_none_for_empty_body(
    client: HttpClient, mocked: responses.RequestsMock, method: str
) -> None:
    mocked.add(method.split("_")[0].upper(), f"{BASE}/invoices/7", status=204)

    assert getattr(client, method)("/invoices/7", {"exported": True}) is None


def test_delete(client: HttpClient, mocked: responses.RequestsMock) -> None:
    mocked.delete(f"{BASE}/invoices/7", status=204)

    client.delete("/invoices/7")

    assert len(mocked.calls) == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(400, APIError), (401, AuthenticationError), (404, NotFoundError), (422, APIError)],
)
def test_non_2xx_raises_typed_api_error(
    client: HttpClient, mocked: responses.RequestsMock, status: int, error_type: type[APIError]
) -> None:
    mocked.get(f"{BASE}/invoices/9", status=status, body="nope")

    with pytest.raises(error_type) as caught:
        client.get_json("/invoices/9", params={"api_key": "hide-me"})

    error = caught.value
    assert (error.method, error.url, error.status_code) == ("GET", f"{BASE}/invoices/9", status)
    assert error.response_body == "nope"
    assert error.params == {"api_key": "***"}


def test_network_failure_becomes_retryable_api_error(
    client: HttpClient, mocked: responses.RequestsMock
) -> None:
    mocked.get(f"{BASE}/invoices", body=requests.ConnectionError("refused"))

    with pytest.raises(APIError, match="ConnectionError") as caught:
        client.get_json("/invoices")

    assert caught.value.status_code is None
    assert caught.value.is_retryable


def test_invalid_json_raises(client: HttpClient, mocked: responses.RequestsMock) -> None:
    mocked.get(f"{BASE}/invoices", body="<html>maintenance</html>", status=200)

    with pytest.raises(APIError, match="not valid JSON"):
        client.get_json("/invoices")


def test_auth_is_applied(mocked: responses.RequestsMock) -> None:
    mocked.get(f"{BASE}/me", json={}, match=[matchers.header_matcher({"Authorization": "Basic dTpw"})])

    with HttpClient(BASE, auth=HTTPBasicAuth("u", "p")) as http:
        http.get_json("/me")


def test_retry_policy_covers_throttling_and_server_errors_for_idempotent_methods() -> None:
    with HttpClient(BASE, retries=4, backoff_factor=1.5) as http:
        retry = http.session.get_adapter(BASE).max_retries  # type: ignore[attr-defined]

    assert retry.total == 4
    assert retry.backoff_factor == 1.5
    assert set(retry.status_forcelist) == RETRY_STATUS_CODES
    assert retry.respect_retry_after_header
    assert "GET" in retry.allowed_methods
    assert "POST" not in retry.allowed_methods
