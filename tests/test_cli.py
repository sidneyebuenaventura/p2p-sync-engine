import os
import runpy
import signal
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from p2p_sync import cli
from p2p_sync.connectors.erp import ErpClient, Vendor
from p2p_sync.connectors.maintenance import MaintenanceClient, MaintenanceDatabase
from p2p_sync.connectors.procurement import Invoice, ProcurementClient, Supplier
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.settings import Settings
from p2p_sync.scheduler import Scheduler


@pytest.fixture(autouse=True)
def logging_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
    for key in list(os.environ):
        if key.startswith("P2P_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    # configure_logging would bolt a handler onto pytest's captured stderr; just record the call
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "configure_logging", lambda level, fmt: calls.append((level, fmt)))
    return calls


@pytest.fixture
def maintenance(tmp_path: Path) -> Iterator[MaintenanceClient]:
    db = MaintenanceDatabase(f"sqlite:///{tmp_path / 'maintenance.db'}")
    db.create_schema()
    yield MaintenanceClient(db)
    db.engine.dispose()


@pytest.fixture
def conn(
    monkeypatch: pytest.MonkeyPatch, procurement: Any, erp: Any, maintenance: MaintenanceClient
) -> cli.Connections:
    connections = cli.Connections(procurement, erp, maintenance)
    monkeypatch.setattr(cli, "connect", lambda settings: connections)
    return connections


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "p2p-sync 0.1.0"


def test_suppliers_go_to_both_systems(
    conn: cli.Connections, procurement: Any, erp: Any, make_supplier: Callable[..., Supplier], capsys: Any
) -> None:
    procurement.supplier_list = [make_supplier(1)]

    assert cli.main(["suppliers"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "suppliers -> erp: create=1" in out
    assert "suppliers -> maintenance: create=1" in out
    assert len(erp.created) == 1
    assert conn.maintenance.contractor_for_supplier(1) is not None


def test_suppliers_dry_run_to_one_target(
    conn: cli.Connections, procurement: Any, erp: Any, make_supplier: Callable[..., Supplier], capsys: Any
) -> None:
    procurement.supplier_list = [make_supplier(1)]

    assert cli.main(["suppliers", "--to", "erp", "--dry-run"]) == cli.EXIT_OK

    assert capsys.readouterr().out.splitlines() == [
        "suppliers -> erp: [dry run] create=1 update=0 skip=0 conflict=0 failed=0"
    ]
    assert erp.created == []


def test_suppliers_to_maintenance_only(
    conn: cli.Connections, procurement: Any, erp: Any, make_supplier: Callable[..., Supplier], capsys: Any
) -> None:
    procurement.supplier_list = [make_supplier(1)]

    assert cli.main(["suppliers", "--to", "maintenance"]) == cli.EXIT_OK

    assert capsys.readouterr().out.startswith("suppliers -> maintenance: create=1")
    assert erp.created == []


def test_invoice_problems_are_listed_and_exit_one(
    conn: cli.Connections, procurement: Any, make_invoice: Callable[..., Invoice], capsys: Any
) -> None:
    procurement.invoice_list = [make_invoice(1, supplier_id=9)]

    assert cli.main(["invoices"]) == cli.EXIT_PROBLEMS

    assert "  invoice INV-1 (id 1): supplier Supplier 9 (9) has no ERP vendor yet" in capsys.readouterr().out


def test_invoices_use_default_gl_account_from_settings(
    conn: cli.Connections,
    procurement: Any,
    erp: Any,
    make_invoice: Callable[..., Invoice],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2P_ERP__DEFAULT_GL_ACCOUNT", "6000")
    erp.vendor_list = [Vendor(vendor_id="V001", name="Supplier 1", external_ref="procurement:supplier:1")]
    procurement.invoice_list = [make_invoice(1)]

    assert cli.main(["invoices"]) == cli.EXIT_OK

    assert [line.gl_account for line in erp.posted[0].lines] == ["6100", "6000"]


def test_bad_config_exits_two(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setenv("P2P_SCHEDULE__INVOICES", "whenever")

    assert cli.main(["invoices"]) == cli.EXIT_CONFIG
    assert "bad configuration" in capsys.readouterr().err


def test_command_that_cannot_start_exits_one(
    conn: cli.Connections, procurement: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    def down(**_: Any) -> list[Supplier]:
        raise P2PSyncError("procurement returned 503")

    monkeypatch.setattr(procurement, "suppliers", down)

    assert cli.main(["suppliers"]) == cli.EXIT_PROBLEMS
    assert "suppliers failed: procurement returned 503" in caplog.text


def test_log_level_flag_wins_over_settings(
    conn: cli.Connections, monkeypatch: pytest.MonkeyPatch, logging_calls: list[tuple[str, str]]
) -> None:
    monkeypatch.setenv("P2P_LOG_FORMAT", "json")

    cli.main(["--log-level", "DEBUG", "suppliers"])
    cli.main(["suppliers"])

    assert logging_calls == [("DEBUG", "json"), ("INFO", "json")]


def test_schedule_runs_both_jobs_and_restores_signal_handlers(
    conn: cli.Connections, procurement: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    before = signal.getsignal(signal.SIGINT)

    def run_once(self: Scheduler, stop: threading.Event | None = None, max_sleep: float = 60.0) -> None:
        for job in self.run_pending():
            ran.append(job.name)
        assert signal.getsignal(signal.SIGINT) is not before

    monkeypatch.setattr(Scheduler, "run_forever", run_once)

    assert cli.main(["schedule", "--run-now"]) == cli.EXIT_OK
    assert ran == ["suppliers", "invoices"]
    assert signal.getsignal(signal.SIGINT) is before


def test_signal_sets_the_stop_event() -> None:
    stop = threading.Event()

    with cli.stop_on_signals(stop):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    assert stop.is_set()


def test_connect_builds_real_clients() -> None:
    conn = cli.connect(Settings())

    assert isinstance(conn.procurement, ProcurementClient)
    assert isinstance(conn.erp, ErpClient)
    assert isinstance(conn.maintenance, MaintenanceClient)
    conn.maintenance.db.engine.dispose()


def test_python_dash_m(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["p2p-sync", "--version"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("p2p_sync", run_name="__main__")

    assert exit_info.value.code == 0
