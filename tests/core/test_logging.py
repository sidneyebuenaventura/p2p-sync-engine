import io
import json
import logging
from collections.abc import Iterator

import pytest

from p2p_sync.core.logging import configure_logging, current_log_context, log_context


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    buffer = io.StringIO()
    yield buffer
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_text_format_appends_context(stream: io.StringIO) -> None:
    configure_logging("INFO", "text", stream=stream)

    with log_context(run_id="r-1", flow="invoices"):
        logging.getLogger("p2p.test").info("hello")

    line = stream.getvalue().strip()
    assert "| INFO    | p2p.test | hello run_id=r-1 flow=invoices" in line


def test_json_format_emits_one_object_per_line(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream=stream)

    with log_context(run_id="r-2"):
        logging.getLogger("p2p.test").warning("careful %s", "now")

    record = json.loads(stream.getvalue())
    assert record["level"] == "WARNING"
    assert record["logger"] == "p2p.test"
    assert record["message"] == "careful now"
    assert record["run_id"] == "r-2"
    assert record["timestamp"].endswith("+00:00")


def test_json_format_includes_exception(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", stream=stream)

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("p2p.test").exception("failed")

    assert "RuntimeError: boom" in json.loads(stream.getvalue())["exception"]


def test_nested_context_merges_and_restores() -> None:
    with log_context(run_id="outer"):
        with log_context(invoice_id=42):
            assert current_log_context() == {"run_id": "outer", "invoice_id": 42}
        assert current_log_context() == {"run_id": "outer"}
    assert current_log_context() == {}


def test_reconfiguring_replaces_handler(stream: io.StringIO) -> None:
    configure_logging("INFO", "text", stream=stream)
    configure_logging("INFO", "text", stream=stream)

    logging.getLogger("p2p.test").info("once")

    assert stream.getvalue().count("once") == 1


def test_level_filters_records(stream: io.StringIO) -> None:
    configure_logging("warning", "text", stream=stream)

    logging.getLogger("p2p.test").info("hidden")

    assert stream.getvalue() == ""
