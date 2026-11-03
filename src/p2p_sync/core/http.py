"""A thin, typed wrapper around ``requests`` shared by every REST connector.

What it adds over a bare ``requests.Session``:

* a base URL, so connectors call ``client.get_json("/invoices")``;
* a default timeout on every request (``requests`` has none by default);
* automatic retries with exponential backoff for throttling (429) and server errors,
  honouring ``Retry-After``, but only for idempotent methods so a POST is never
  sent twice;
* failures raised as :class:`~p2p_sync.core.errors.APIError` with full request context.

Authentication is pluggable through any ``requests.auth.AuthBase``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

import requests
from requests.adapters import HTTPAdapter
from requests.auth import AuthBase
from urllib3.util.retry import Retry

from p2p_sync import __version__
from p2p_sync.core.errors import APIError

logger = logging.getLogger(__name__)

RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class HttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        backoff_factor: float = 0.5,
        auth: AuthBase | None = None,
        headers: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.auth = auth
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": f"p2p-sync-engine/{__version__}"}
        )
        if headers:
            self.session.headers.update(headers)

        retry = Retry(
            total=retries,
            backoff_factor=backoff_factor,
            status_forcelist=RETRY_STATUS_CODES,
            allowed_methods=Retry.DEFAULT_ALLOWED_METHODS,  # idempotent methods only
            respect_retry_after_header=True,
            raise_on_status=False,  # hand back the final response so we can raise APIError
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- lifecycle ---------------------------------------------------------------------------

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- requests ----------------------------------------------------------------------------

    def url_for(self, path: str) -> str:
        """Resolve ``path`` against the base URL. Absolute URLs (e.g. pagination links) pass through."""
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        """Send a request and return the response, raising ``APIError`` on failure or non-2xx."""
        method = method.upper()
        url = self.url_for(path)
        started = time.perf_counter()
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise APIError(
                f"{method} request failed: {exc.__class__.__name__}",
                method=method,
                url=url,
                params=params,
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.debug("%s %s -> %s in %.0f ms", method, url, response.status_code, elapsed_ms)

        if not response.ok:
            raise APIError.for_status(
                response.status_code,
                f"{method} request returned HTTP {response.status_code}",
                method=method,
                url=url,
                params=params,
                response_body=response.text,
            )
        return response

    def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return _json_or_none(self.request("GET", path, params=params))

    def post_json(self, path: str, payload: Any, *, params: Mapping[str, Any] | None = None) -> Any:
        return _json_or_none(self.request("POST", path, json=payload, params=params))

    def put_json(self, path: str, payload: Any, *, params: Mapping[str, Any] | None = None) -> Any:
        return _json_or_none(self.request("PUT", path, json=payload, params=params))

    def patch_json(self, path: str, payload: Any, *, params: Mapping[str, Any] | None = None) -> Any:
        return _json_or_none(self.request("PATCH", path, json=payload, params=params))

    def delete(self, path: str, *, params: Mapping[str, Any] | None = None) -> None:
        self.request("DELETE", path, params=params)


def _json_or_none(response: requests.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise APIError(
            "response was not valid JSON",
            method=response.request.method,
            url=response.url,
            status_code=response.status_code,
            response_body=response.text,
        ) from exc
