from collections.abc import Iterator

import pytest
import requests
import responses
from responses import matchers

from p2p_sync.connectors.procurement.auth import ClientCredentialsAuth
from p2p_sync.core.errors import AuthenticationError

OAUTH_URL = "https://procurement.test/oauth2/token"
API = "https://procurement.test/api/ping"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as rsps:
        yield rsps


def token_response(mocked: responses.RequestsMock, token: str, expires_in: int = 3600) -> None:
    mocked.post(OAUTH_URL, json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"})


def test_fetches_token_with_client_credentials(mocked: responses.RequestsMock) -> None:
    mocked.post(
        OAUTH_URL,
        json={"access_token": "t1", "expires_in": 3600},
        match=[
            matchers.urlencoded_params_matcher(
                {
                    "grant_type": "client_credentials",
                    "client_id": "id",
                    "client_secret": "s",
                    "scope": "invoices",
                }
            )
        ],
    )
    mocked.get(API, json={}, match=[matchers.header_matcher({"Authorization": "Bearer t1"})])

    requests.get(API, auth=ClientCredentialsAuth(OAUTH_URL, "id", "s", scope="invoices"), timeout=5)


def test_caches_token_until_close_to_expiry(mocked: responses.RequestsMock) -> None:
    clock = Clock()
    auth = ClientCredentialsAuth(OAUTH_URL, "id", "s", clock=clock, leeway_seconds=60)
    token_response(mocked, "t1", expires_in=300)
    token_response(mocked, "t2", expires_in=300)

    assert auth.token() == "t1"
    clock.now += 239
    assert auth.token() == "t1"
    clock.now += 1  # 240s in: inside the 60s leeway, so refresh
    assert auth.token() == "t2"


def test_retries_once_with_new_token_after_401(mocked: responses.RequestsMock) -> None:
    token_response(mocked, "stale")
    token_response(mocked, "fresh")
    mocked.get(API, status=401, match=[matchers.header_matcher({"Authorization": "Bearer stale"})])
    mocked.get(API, json={"ok": True}, match=[matchers.header_matcher({"Authorization": "Bearer fresh"})])

    response = requests.get(API, auth=ClientCredentialsAuth(OAUTH_URL, "id", "s"), timeout=5)

    assert response.json() == {"ok": True}
    assert [r.status_code for r in response.history] == [401]


def test_gives_up_after_second_401(mocked: responses.RequestsMock) -> None:
    token_response(mocked, "t1")
    token_response(mocked, "t2")
    mocked.get(API, status=401)
    mocked.get(API, status=401)

    response = requests.get(API, auth=ClientCredentialsAuth(OAUTH_URL, "id", "s"), timeout=5)

    assert response.status_code == 401
    assert len(mocked.calls) == 4  # token, api, token, api - no third attempt


def test_rejected_credentials_raise_authentication_error(mocked: responses.RequestsMock) -> None:
    mocked.post(OAUTH_URL, status=400, json={"error": "invalid_client"})

    with pytest.raises(AuthenticationError, match="token request was rejected") as caught:
        ClientCredentialsAuth(OAUTH_URL, "id", "wrong").token()

    assert caught.value.status_code == 400
    assert "wrong" not in str(caught.value)


def test_network_error_on_token_request(mocked: responses.RequestsMock) -> None:
    mocked.post(OAUTH_URL, body=requests.ConnectionError("refused"))

    with pytest.raises(AuthenticationError, match="ConnectionError"):
        ClientCredentialsAuth(OAUTH_URL, "id", "s").token()
