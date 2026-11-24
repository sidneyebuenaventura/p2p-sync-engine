"""Command line entry point.

    p2p-sync suppliers [--to erp|maintenance] [--dry-run]
    p2p-sync invoices [--dry-run]
    p2p-sync schedule [--run-now]

Exit codes: 0 when everything synced, 1 when some records failed or need a person,
2 for bad configuration. A scheduler or pipeline can then tell "fix the config" apart
from "go and look at the integration status in procurement".
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Any

from pydantic import ValidationError

from p2p_sync import __version__
from p2p_sync.connectors.erp import APInvoice, ErpClient
from p2p_sync.connectors.maintenance import MaintenanceClient
from p2p_sync.connectors.procurement import Invoice, ProcurementClient, Supplier
from p2p_sync.core.errors import P2PSyncError
from p2p_sync.core.logging import configure_logging, log_context
from p2p_sync.core.settings import Settings
from p2p_sync.flows import InvoiceSync, SupplierContractorSync, SupplierVendorSync, suppliers_to_sync
from p2p_sync.scheduler import Scheduler
from p2p_sync.sync import SyncReport

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CONFIG = 2

SUPPLIER_TARGETS = ("erp", "maintenance")


@dataclass
class Connections:
    procurement: ProcurementClient
    erp: ErpClient
    maintenance: MaintenanceClient


def connect(settings: Settings) -> Connections:
    timeout = settings.http_timeout_seconds
    return Connections(
        procurement=ProcurementClient.from_settings(settings.procurement, timeout=timeout),
        erp=ErpClient.from_settings(settings.erp, timeout=timeout),
        maintenance=MaintenanceClient.from_settings(settings.maintenance),
    )


def sync_suppliers(
    conn: Connections, targets: Sequence[str] = SUPPLIER_TARGETS, *, dry_run: bool = False
) -> dict[str, SyncReport[Supplier, Any]]:
    suppliers = suppliers_to_sync(conn.procurement)  # fetched once, used for both targets
    reports: dict[str, SyncReport[Supplier, Any]] = {}
    if "erp" in targets:
        reports["erp"] = SupplierVendorSync(conn.procurement, conn.erp, dry_run=dry_run).run(suppliers)
    if "maintenance" in targets:
        contractors = SupplierContractorSync(conn.procurement, conn.maintenance, dry_run=dry_run)
        reports["maintenance"] = contractors.run(suppliers)
    return reports


def sync_invoices(
    conn: Connections, settings: Settings, *, dry_run: bool = False
) -> SyncReport[Invoice, APInvoice]:
    flow = InvoiceSync(
        conn.procurement, conn.erp, default_gl_account=settings.erp.default_gl_account, dry_run=dry_run
    )
    return flow.run()


def build_scheduler(conn: Connections, settings: Settings, *, run_now: bool = False) -> Scheduler:
    scheduler = Scheduler(tz=settings.tz)
    # Two independent jobs on purpose. An invoice whose supplier isn't in the ERP yet fails
    # this round and goes through on the next one, after the supplier job has caught up.
    scheduler.add_job(
        lambda: sync_suppliers(conn), settings.schedule.suppliers, name="suppliers", run_on_start=run_now
    )
    scheduler.add_job(
        lambda: sync_invoices(conn, settings),
        settings.schedule.invoices,
        name="invoices",
        run_on_start=run_now,
    )
    return scheduler


# commands


def _suppliers(args: argparse.Namespace, settings: Settings, conn: Connections) -> int:
    targets = SUPPLIER_TARGETS if args.to == "all" else (args.to,)
    reports = sync_suppliers(conn, targets, dry_run=args.dry_run)
    for target, report in reports.items():
        _print_report(f"suppliers -> {target}", report, lambda s: f"supplier {s.name} (id {s.id})")
    return _exit_code(reports.values())


def _invoices(args: argparse.Namespace, settings: Settings, conn: Connections) -> int:
    report = sync_invoices(conn, settings, dry_run=args.dry_run)
    _print_report("invoices -> erp", report, lambda i: f"invoice {i.invoice_number} (id {i.id})")
    return _exit_code([report])


def _schedule(args: argparse.Namespace, settings: Settings, conn: Connections) -> int:
    scheduler = build_scheduler(conn, settings, run_now=args.run_now)
    stop = threading.Event()
    with stop_on_signals(stop):
        scheduler.run_forever(stop)
    return EXIT_OK


@contextmanager
def stop_on_signals(stop: threading.Event) -> Iterator[None]:
    """Ctrl+C or a container stop lets the running job finish, then exits cleanly."""

    def request_stop(signum: int, frame: FrameType | None) -> None:
        logger.info("got signal %s, stopping after the current job", signum)
        stop.set()

    wanted = (signal.SIGINT, signal.SIGTERM)
    previous = {sig: signal.getsignal(sig) for sig in wanted}
    for sig in wanted:
        signal.signal(sig, request_stop)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _print_report[S](label: str, report: SyncReport[S, Any], describe: Callable[[S], str]) -> None:
    print(f"{label}: {report.summary()}")
    for result in report.results:
        if not result.ok:
            print(f"  {describe(result.source)}: {result.error or result.reason}")


def _exit_code(reports: Iterable[SyncReport[Any, Any]]) -> int:
    return EXIT_OK if all(report.ok for report in reports) else EXIT_PROBLEMS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p2p-sync", description="Sync suppliers and invoices between procurement, ERP and maintenance."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="overrides P2P_LOG_LEVEL"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    suppliers = commands.add_parser("suppliers", help="push procurement suppliers to the ERP and maintenance")
    suppliers.add_argument("--to", choices=["all", *SUPPLIER_TARGETS], default="all")
    suppliers.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    suppliers.set_defaults(handler=_suppliers)

    invoices = commands.add_parser("invoices", help="post approved procurement invoices to the ERP")
    invoices.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    invoices.set_defaults(handler=_invoices)

    schedule = commands.add_parser(
        "schedule", help="run every sync on its P2P_SCHEDULE__* cron until stopped"
    )
    schedule.add_argument("--run-now", action="store_true", help="run each job once at startup as well")
    schedule.set_defaults(handler=_schedule)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"p2p-sync: bad configuration\n{exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(args.log_level or settings.log_level, settings.log_format)
    handler: Callable[[argparse.Namespace, Settings, Connections], int] = args.handler
    with log_context(command=args.command):
        try:
            return handler(args, settings, connect(settings))
        except P2PSyncError as exc:
            # e.g. procurement was down when listing suppliers: nothing synced, nothing half done
            logger.error("%s failed: %s", args.command, exc)
            return EXIT_PROBLEMS
