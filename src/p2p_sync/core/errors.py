"""Exceptions used across the package."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

_SENSITIVE_KEYS = ("password", "secret", "token", "api_key", "apikey", "authorization", "client_secret")
_BODY_PREVIEW_CHARS = 500
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class P2PSyncError(Exception):
    pass


class ConfigurationError(P2PSyncError):
    pass


class APIError(P2PSyncError):
    # Keeps the request details so the log line alone is enough to debug a failed call.
    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        url: str | None = None,
        status_code: int | None = None,
        params: Mapping[str, Any] | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.method = method.upper() if method else None
        self.url = url
        self.status_code = status_code
        self.params = redact(params) if params else {}
        self.response_body = response_body

    @classmethod
    def for_status(cls, status_code: int, message: str, **context: Any) -> APIError:
        error_type: type[APIError]
        if status_code in (401, 403):
            error_type = AuthenticationError
        elif status_code == 404:
            error_type = NotFoundError
        else:
            error_type = APIError
        return error_type(message, status_code=status_code, **context)

    @property
    def is_retryable(self) -> bool:
        # no status code means we never got a response (timeout, DNS, refused...)
        return self.status_code is None or self.status_code in _RETRYABLE_STATUS

    def with_context(self, **context: Any) -> Self:
        fields: dict[str, Any] = {
            "method": self.method,
            "url": self.url,
            "status_code": self.status_code,
            "params": self.params,
            "response_body": self.response_body,
        }
        return type(self)(self.message, **{**fields, **context})

    def __str__(self) -> str:
        parts = [self.message]
        request = " ".join(p for p in (self.method, self.url) if p)
        if request or self.status_code is not None:
            status = f" -> {self.status_code}" if self.status_code is not None else ""
            parts.append(f"[{request}{status}]")
        if self.params:
            parts.append(f"params={self.params}")
        if self.response_body:
            body = self.response_body
            if len(body) > _BODY_PREVIEW_CHARS:
                body = body[:_BODY_PREVIEW_CHARS] + "…"
            parts.append(f"body={body!r}")
        return " ".join(parts)


class AuthenticationError(APIError):
    pass


class NotFoundError(APIError):
    pass


def redact(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: "***" if any(marker in key.lower() for marker in _SENSITIVE_KEYS) else value
        for key, value in values.items()
        if value is not None
    }
