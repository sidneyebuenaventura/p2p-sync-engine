"""Logging setup with per-run context.

``configure_logging`` installs a single handler on the root logger that writes either
human-readable text or one JSON object per line. ``log_context`` attaches fields such as
``run_id`` or ``invoice_id`` to every record emitted inside its block, including records
from libraries, without threading a logger adapter through the call stack::

    with log_context(run_id="2026-11-03T08:00", flow="invoices"):
        logger.info("starting sync")   # -> ... run_id=2026-11-03T08:00 flow=invoices
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
    """Attach ``fields`` to every log record emitted inside the block. Blocks can be nested."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def current_log_context() -> dict[str, Any]:
    return dict(_context.get())


class ContextFilter(logging.Filter):
    """Copy the active log context onto each record as ``record.context``."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = current_log_context()
        record.context = context
        record.context_suffix = "".join(f" {key}={value}" for key, value in context.items())
        return True


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object."""

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
    """Configure the root logger. Safe to call more than once; the previous handler is replaced."""
    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(existing)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT))

    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party HTTP libraries are noisy at INFO; keep them at WARNING unless debugging.
    if level.upper() != "DEBUG":
        for noisy in ("urllib3", "azure"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
