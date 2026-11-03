import pytest

from p2p_sync.core.errors import (
    APIError,
    AuthenticationError,
    NotFoundError,
    P2PSyncError,
    redact,
)


def test_api_error_is_a_package_error() -> None:
    assert issubclass(APIError, P2PSyncError)


def test_str_includes_request_context() -> None:
    error = APIError(
        "create invoice failed",
        method="post",
        url="https://erp.example.com/invoices",
        status_code=422,
        response_body='{"error": "missing vendor"}',
    )

    assert str(error) == (
        "create invoice failed [POST https://erp.example.com/invoices -> 422] "
        'body=\'{"error": "missing vendor"}\''
    )


def test_str_without_context_is_just_the_message() -> None:
    assert str(APIError("boom")) == "boom"


def test_long_bodies_are_truncated() -> None:
    error = APIError("bad", response_body="x" * 2_000)

    assert len(str(error)) < 600
    assert str(error).endswith("…'")


def test_params_are_redacted_and_none_values_dropped() -> None:
    error = APIError(
        "bad",
        params={"page": 2, "api_key": "k", "Client_Secret": "s", "filter": None},
    )

    assert error.params == {"page": 2, "api_key": "***", "Client_Secret": "***"}
    assert "k'" not in str(error)


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [(401, AuthenticationError), (403, AuthenticationError), (404, NotFoundError), (500, APIError)],
)
def test_for_status_picks_specific_subclass(status: int, expected_type: type[APIError]) -> None:
    error = APIError.for_status(status, "failed", url="https://x")

    assert type(error) is expected_type
    assert error.status_code == status
    assert error.url == "https://x"


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(None, True), (429, True), (503, True), (400, False), (404, False), (422, False)],
)
def test_is_retryable(status: int | None, retryable: bool) -> None:
    assert APIError("x", status_code=status).is_retryable is retryable


def test_with_context_preserves_type_and_fills_fields() -> None:
    original = NotFoundError("missing", status_code=404)

    enriched = original.with_context(method="get", url="https://x/1")

    assert type(enriched) is NotFoundError
    assert (enriched.method, enriched.url, enriched.status_code) == ("GET", "https://x/1", 404)


def test_redact_is_case_insensitive() -> None:
    assert redact({"Authorization": "Bearer abc", "PASSWORD": "p", "id": 1}) == {
        "Authorization": "***",
        "PASSWORD": "***",
        "id": 1,
    }
