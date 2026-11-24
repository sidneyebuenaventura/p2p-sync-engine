"""The whole thing: p2p-sync against the mock APIs, loaded with the Acme sample data.

Real HTTP, real SQLite, real CLI. Each assertion maps to one of the cases the sample
data was written to show.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

from p2p_sync import cli
from p2p_sync.connectors.maintenance import MaintenanceClient, MaintenanceDatabase
from p2p_sync.demo import data, erp_api, procurement_api
from p2p_sync.demo.__main__ import env_file
from p2p_sync.demo.erp_api import ErpStore
from p2p_sync.demo.procurement_api import ProcurementStore
from p2p_sync.demo.server import BackgroundServer

TODAY = date.today()


@dataclass
class Demo:
    procurement: ProcurementStore
    erp: ErpStore
    maintenance: MaintenanceClient

    def supplier_status(self, supplier_id: int) -> tuple[str, str]:
        status = self.procurement.suppliers[supplier_id]["integration_status"]
        return status["status"], status["message"]

    def invoice_status(self, invoice_id: int) -> tuple[str, str]:
        status = self.procurement.invoices[invoice_id]["integration_status"]
        return status["status"], status["message"]


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Demo]:
    for key in list(os.environ):
        if key.startswith("P2P_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_logging", lambda level, fmt: None)

    db = MaintenanceDatabase(f"sqlite:///{tmp_path / 'maintenance.db'}")
    db.create_schema()
    data.seed_maintenance(db, TODAY)
    procurement_store, erp_store = data.procurement_store(TODAY), data.erp_store(TODAY)

    with (
        BackgroundServer(procurement_api.create_app(procurement_store)) as procurement,
        BackgroundServer(erp_api.create_app(erp_store)) as erp,
    ):
        for line in env_file(procurement.url, erp.url).splitlines()[1:]:
            key, _, value = line.partition("=")
            monkeypatch.setenv(key, value)
        yield Demo(procurement_store, erp_store, MaintenanceClient(db))

    db.engine.dispose()


def test_supplier_sync(demo: Demo, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["suppliers"]) == cli.EXIT_PROBLEMS

    out = capsys.readouterr().out
    assert "suppliers -> erp: create=2 update=0 skip=0 conflict=0 failed=1" in out
    assert "supplier Gamma Office Supplies (id 3): no ERP payment terms code for 'Net 90'" in out

    # Acme Pumps is new: a fresh vendor
    assert demo.supplier_status(1) == ("success", "ERP vendor V0002")
    assert demo.erp.vendors["V0002"]["external_ref"] == "procurement:supplier:1"
    # Beta was keyed in by hand: that vendor is taken over, not duplicated
    assert demo.supplier_status(2) == ("success", "ERP vendor V0001")
    assert demo.erp.vendors["V0001"]["name"] == "Beta Electrical Services"
    assert demo.erp.vendors["V0001"]["external_ref"] == "procurement:supplier:2"
    # Gamma's terms have no ERP code, so it stays out and AP can see why
    assert demo.supplier_status(3)[0] == "error"
    # Delta is inactive and was never sent anywhere
    assert "integration_status" not in demo.procurement.suppliers[4]
    assert len(demo.erp.vendors) == 2

    # maintenance: Beta's hand-made contractor keeps its code, the others are new
    contractors = {c.procurement_supplier_id: c.code for c in demo.maintenance.contractors()}
    assert contractors == {1: "PS000001", 2: "BETA-EL", 3: "PS000003"}


def test_invoice_sync(demo: Demo, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["suppliers"])
    capsys.readouterr()

    assert cli.main(["invoices"]) == cli.EXIT_PROBLEMS

    assert "invoices -> erp: create=1 update=0 skip=2 conflict=0 failed=2" in capsys.readouterr().out

    # AP-24017 posts; its labour line had no account code and got the default
    assert demo.invoice_status(101) == ("success", "ERP AP invoice 1003")
    assert [line["gl_account"] for line in demo.erp.ap_invoices[1003]["lines"]] == ["6100", "6000"]
    assert demo.procurement.invoices[101]["exported"] is True

    # AP-23988 is dated in a closed period
    closed_day = TODAY - timedelta(days=120)
    assert demo.invoice_status(102) == (
        "error",
        f"period {closed_day:%Y/%m} is closed ({closed_day.isoformat()})",
    )

    # BE-7781 was already keyed by hand with the same total
    assert demo.invoice_status(103) == ("success", "already in ERP as AP invoice 1001 (keyed by hand)")
    # BE-7790 was keyed by hand with the wrong total: left for a person
    assert demo.invoice_status(104) == (
        "error",
        "already in ERP as AP invoice 1002 with total 12600.00, procurement has 14000.00",
    )
    assert demo.procurement.invoices[104]["exported"] is False

    # GOS-0042's supplier never made it into the ERP
    assert demo.invoice_status(105) == ("error", "supplier Gamma Office Supplies (3) has no ERP vendor yet")

    assert sorted(demo.erp.ap_invoices) == [1001, 1002, 1003]


def test_running_everything_again_changes_nothing(demo: Demo, capsys: pytest.CaptureFixture[str]) -> None:
    for _ in range(2):
        cli.main(["suppliers"])
        cli.main(["invoices"])
    out = capsys.readouterr().out

    assert len(demo.erp.vendors) == 2
    assert sorted(demo.erp.ap_invoices) == [1001, 1002, 1003]
    assert len(demo.maintenance.contractors()) == 3
    assert "suppliers -> erp: create=0 update=0 skip=2 conflict=0 failed=1" in out
