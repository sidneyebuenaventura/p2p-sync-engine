"""Logging setup.

log_context() tags every log line inside a block with extra fields (run_id, invoice
number, ...) so you don't have to pass a logger adapter around:

    with log_context(run_id=run_id, flow="invoices"):
        logger.info("starting sync")
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

_context: ContextVar[Mapping[str, Any]] = ContextVar("p2p_log_context", default={})  # noqa: B039 - never mutated

_HANDLER_NAME = "p2p_sync"
_TEXT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s%(context_suffix)s"


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def current_log_context() -> dict[str, Any]:
    return dict(_context.get())


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = current_log_context()
        record.context = context
        record.context_suffix = "".join(f" {key}={value}" for key, value in context.items())
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **getattr(record, "context", {}),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    level: str = "INFO",
    fmt: Literal["text", "json"] = "text",
    stream: TextIO | None = None,
) -> None:
    # Safe to call twice - we swap out our own handler instead of stacking another one.
    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(existing)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT))

    root.addHandler(handler)
    root.setLevel(level.upper())

    # urllib3 logs every retry at INFO, too chatty unless we're debugging
    if level.upper() != "DEBUG":
        for noisy in ("urllib3", "azure"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
