import argparse
import socket
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

from p2p_sync.connectors.erp import FiscalPeriod
from p2p_sync.connectors.maintenance import MaintenanceClient, MaintenanceDatabase
from p2p_sync.connectors.procurement import Invoice, Supplier
from p2p_sync.demo import data
from p2p_sync.demo.__main__ import ENV_MARKER, env_file, main, run, write_env

TODAY = date(2026, 11, 24)


def args(folder: Path, procurement_port: int = 0, erp_port: int = 0) -> argparse.Namespace:
    return argparse.Namespace(
        folder=str(folder), host="127.0.0.1", procurement_port=procurement_port, erp_port=erp_port
    )


def stopped() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


class StopOnSecondPoll(threading.Event):
    """Keeps the demo waiting for exactly one poll, then says stop."""

    def __init__(self) -> None:
        super().__init__()
        self.polls = 0

    def wait(self, timeout: float | None = None) -> bool:
        self.polls += 1
        return self.polls > 1


def test_sample_data_is_valid_for_the_real_models() -> None:
    store = data.procurement_store(TODAY)

    suppliers = [Supplier.model_validate(s) for s in store.suppliers.values()]
    invoices = [Invoice.model_validate(i) for i in store.invoices.values()]

    assert len(suppliers) == 4
    assert [i.invoice_number for i in invoices] == ["AP-24017", "AP-23988", "BE-7781", "BE-7790", "GOS-0042"]
    assert invoices[0].supplier.name == "Acme Pumps Inc."


@pytest.mark.parametrize(
    ("day", "closed"),
    [
        (TODAY, False),
        (date(2026, 10, 1), False),
        (date(2026, 9, 30), True),
        (TODAY - timedelta(days=120), True),
    ],
)
def test_this_month_and_last_month_are_open(day: date, closed: bool) -> None:
    periods = [FiscalPeriod.model_validate(p) for p in data.fiscal_periods(TODAY)]

    [period] = [p for p in periods if p.contains(day)]
    assert period.closed is closed


def test_write_env_never_overwrites_a_users_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"

    assert write_env(path, env_file("http://a", "http://b"))
    assert write_env(path, env_file("http://c", "http://d"))  # ours, so replaced
    assert "http://c" in path.read_text(encoding="utf-8")

    path.write_text("P2P_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    assert not write_env(path, env_file("http://a", "http://b"))
    assert path.read_text(encoding="utf-8") == "P2P_LOG_LEVEL=DEBUG\n"


def test_run_seeds_everything_and_writes_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "maintenance.db").write_text("stale", encoding="utf-8")

    stop = StopOnSecondPoll()

    assert run(args(tmp_path), stop=stop, today=TODAY) == 0
    assert stop.polls == 2

    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert env.startswith(ENV_MARKER)
    assert "P2P_ERP__DEFAULT_GL_ACCOUNT=6000" in env
    db = MaintenanceDatabase(f"sqlite:///{tmp_path / 'maintenance.db'}")
    maintenance = MaintenanceClient(db)
    assert [c.code for c in maintenance.contractors()] == ["BETA-EL"]
    assert [wo.number for wo in maintenance.open_work_orders()] == ["WO-1001", "WO-1002"]
    db.engine.dispose()
    assert "p2p-sync suppliers" in capsys.readouterr().out


def test_run_prints_settings_when_a_users_env_is_in_the_way(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("P2P_LOG_LEVEL=DEBUG\n", encoding="utf-8")

    run(args(tmp_path), stop=stopped(), today=TODAY)

    out = capsys.readouterr().out
    assert "left alone" in out
    assert "P2P_PROCUREMENT__CLIENT_ID=acme-integration" in out


@pytest.mark.parametrize("which", ["procurement_port", "erp_port"])
def test_port_in_use_is_explained(tmp_path: Path, capsys: pytest.CaptureFixture[str], which: str) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]

        assert run(args(tmp_path, **{which: port}), stop=stopped(), today=TODAY) == 1

    assert "Is the demo already running?" in capsys.readouterr().out


def test_main_parses_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr("p2p_sync.demo.__main__.run", lambda parsed: seen.append(parsed) or 0)

    assert main(["--folder", str(tmp_path), "--erp-port", "9002"]) == 0
    assert (seen[0].procurement_port, seen[0].erp_port) == (8001, 9002)
