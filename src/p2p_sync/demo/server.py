"""Run an ASGI app with uvicorn on a background thread."""

from __future__ import annotations

import socket
import threading
import time
from types import TracebackType
from typing import Any, Self

import uvicorn


class BackgroundServer:
    # Binds the socket up front so port=0 works: the OS picks a free port and we know it
    # before the server starts. Tests rely on that to run in parallel without clashes.

    def __init__(self, app: Any, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.bind((host, port))
        except OSError:
            self._socket.close()
            raise
        self.host = host
        self.port: int = self._socket.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._socket]},
            daemon=True,
            name=f"uvicorn:{self.port}",
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 10.0) -> Self:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"server on port {self.port} did not start")
            time.sleep(0.02)
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._server.should_exit = True
        if self._thread.is_alive():
            self._thread.join(timeout)
        self._socket.close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()
