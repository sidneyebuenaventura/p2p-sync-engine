"""Exception hierarchy shared by every connector and sync flow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

_SENSITIVE_KEYS = ("password", "secret", "token", "api_key", "apikey", "authorization", "client_secret")
_BODY_PREVIEW_CHARS = 500
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class P2PSyncError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(P2PSyncError):
    """Required configuration is missing or invalid."""


class APIError(P2PSyncError):
    """A call to an external system failed.

    Carries enough request context to diagnose the failure from a log line alone, while
    redacting anything that looks like a credential.
    """

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
        """Build the most specific subclass for an HTTP status code."""
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
        """True for transient failures: network errors, throttling and server errors."""
        return self.status_code is None or self.status_code in _RETRYABLE_STATUS

    def with_context(self, **context: Any) -> Self:
        """Return a copy with extra request context filled in."""
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
    """Credentials were rejected (HTTP 401/403)."""


class NotFoundError(APIError):
    """The requested resource does not exist (HTTP 404)."""


def redact(values: Mapping[str, Any]) -> dict[str, Any]:
    """Mask values whose key looks like a credential."""
    return {
        key: "***" if any(marker in key.lower() for marker in _SENSITIVE_KEYS) else value
        for key, value in values.items()
        if value is not None
    }
