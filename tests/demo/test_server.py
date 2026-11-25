import pytest
import requests
import uvicorn
from fastapi import FastAPI

from p2p_sync.demo.server import BackgroundServer


def test_serves_on_a_free_port_until_stopped() -> None:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> str:
        return "pong"

    with BackgroundServer(app) as server:
        assert server.port != 0
        assert requests.get(f"{server.url}/ping", timeout=5).json() == "pong"

    with pytest.raises(requests.ConnectionError):
        requests.get(f"{server.url}/ping", timeout=1)


def test_raises_when_the_server_thread_dies_before_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)
    server = BackgroundServer(FastAPI())

    with pytest.raises(RuntimeError, match="did not start"):
        server.start()
    server.stop()


def test_port_in_use_raises_without_leaking_a_socket() -> None:
    # Linux says "Address already in use", Windows "Only one usage of each socket address"
    with BackgroundServer(FastAPI()) as first, pytest.raises(OSError, match=r"(?i)in use|only one usage"):
        BackgroundServer(FastAPI(), port=first.port)


def test_stop_before_start_is_harmless() -> None:
    BackgroundServer(FastAPI()).stop()
