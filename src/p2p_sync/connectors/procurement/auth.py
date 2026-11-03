"""OAuth2 client-credentials auth for requests."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import requests
from requests.auth import AuthBase

from p2p_sync.core.errors import AuthenticationError


class ClientCredentialsAuth(AuthBase):
    """Adds a bearer token, fetching and caching it as needed.

    If the API answers 401 (token revoked or expired early) we get a new token and
    replay the request once.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scope: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        leeway_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self._session = session or requests.Session()
        self._timeout = timeout
        self._leeway = leeway_seconds
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self.token()}"
        request.hooks["response"].append(self._retry_on_401)
        return request

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires_at:
                self._token, lifetime = self._fetch_token()
                self._expires_at = self._clock() + max(0.0, lifetime - self._leeway)
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _fetch_token(self) -> tuple[str, float]:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._client_secret,
        }
        if self.scope:
            data["scope"] = self.scope
        try:
            response = self._session.post(self.token_url, data=data, timeout=self._timeout)
        except requests.RequestException as exc:
            raise AuthenticationError(
                f"token request failed: {exc.__class__.__name__}", method="POST", url=self.token_url
            ) from exc
        if not response.ok:
            raise AuthenticationError(
                "token request was rejected",
                method="POST",
                url=self.token_url,
                status_code=response.status_code,
                response_body=response.text,
            )
        body = response.json()
        return body["access_token"], float(body.get("expires_in", 3600))

    def _retry_on_401(self, response: requests.Response, **kwargs: Any) -> requests.Response:
        if response.status_code != 401 or response.connection is None:
            return response

        self.invalidate()
        retry = response.request.copy()
        # drop our hook from the copy so a second 401 is returned as-is (no retry loop)
        retry.hooks["response"] = [h for h in retry.hooks["response"] if h != self._retry_on_401]
        retry.headers["Authorization"] = f"Bearer {self.token()}"

        response.close()
        new_response: requests.Response = response.connection.send(retry, **kwargs)
        new_response.history.append(response)
        new_response.request = retry
        return new_response
