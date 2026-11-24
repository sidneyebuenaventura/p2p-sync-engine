"""Run the Acme demo.

    mkdir acme-demo
    cd acme-demo
    python -m p2p_sync.demo

This writes a .env that points p2p-sync at the mock APIs, creates maintenance.db with
sample contractors and work orders, and serves the procurement and ERP mocks until
Ctrl+C. Then, in a second terminal in the same folder:

    p2p-sync suppliers
    p2p-sync invoices

Every start is a fresh copy of the sample data.
"""

from __future__ import annotations

import argparse
import threading
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from p2p_sync.cli import stop_on_signals
from p2p_sync.connectors.maintenance import MaintenanceDatabase
from p2p_sync.demo import data, erp_api, procurement_api
from p2p_sync.demo.server import BackgroundServer

ENV_MARKER = "# written by python -m p2p_sync.demo, safe to delete"


def env_file(procurement_url: str, erp_url: str) -> str:
    values = {
        "P2P_PROCUREMENT__BASE_URL": procurement_url,
        "P2P_PROCUREMENT__CLIENT_ID": procurement_api.DEMO_CLIENT_ID,
        "P2P_PROCUREMENT__CLIENT_SECRET": procurement_api.DEMO_CLIENT_SECRET,
        "P2P_ERP__BASE_URL": erp_url,
        "P2P_ERP__COMPANY": "ACME",
        "P2P_ERP__API_KEY": erp_api.DEMO_API_KEY,
        "P2P_ERP__USERNAME": erp_api.DEMO_USERNAME,
        "P2P_ERP__PASSWORD": erp_api.DEMO_PASSWORD,
        "P2P_ERP__DEFAULT_GL_ACCOUNT": "6000",
        "P2P_MAINTENANCE__DATABASE_URL": "sqlite:///maintenance.db",
    }
    return "\n".join([ENV_MARKER, *(f"{key}={value}" for key, value in values.items())]) + "\n"


def write_env(path: Path, content: str) -> bool:
    """Write the demo .env, but never over one somebody wrote by hand."""
    if path.exists() and not path.read_text(encoding="utf-8").startswith(ENV_MARKER):
        return False
    path.write_text(content, encoding="utf-8")
    return True


def run(args: argparse.Namespace, *, stop: threading.Event | None = None, today: date | None = None) -> int:
    today = today or date.today()
    folder = Path(args.folder)

    apps = [
        (procurement_api.create_app(data.procurement_store(today)), args.procurement_port),
        (erp_api.create_app(data.erp_store(today)), args.erp_port),
    ]
    servers: list[BackgroundServer] = []
    try:
        for app, port in apps:
            servers.append(BackgroundServer(app, host=args.host, port=port))
    except OSError as exc:
        for server in servers:
            server.stop()
        print(f"Could not start the mock APIs: {exc}")
        print("Is the demo already running? Pick other ports with --procurement-port / --erp-port.")
        return 1
    procurement, erp = servers

    db_path = folder / "maintenance.db"
    db_path.unlink(missing_ok=True)
    db = MaintenanceDatabase(f"sqlite:///{db_path}")
    db.create_schema()
    data.seed_maintenance(db, today)
    db.engine.dispose()

    env = env_file(procurement.url, erp.url)
    env_written = write_env(folder / ".env", env)

    with procurement, erp:
        print(
            f"procurement mock  {procurement.url}   (records and sync results: {procurement.url}/demo/status)"
        )
        print(f"erp mock          {erp.url}   (vendors and AP invoices: {erp.url}/demo/status)")
        print(f"maintenance db    {db_path.resolve()}")
        if env_written:
            print(f"settings          {(folder / '.env').resolve()}")
        else:
            print("\nThere is already a .env here that the demo didn't write, so it was left alone.")
            print("Point p2p-sync at the mocks with these settings:\n")
            print(env)
        print("\nIn another terminal, in this folder, run:  p2p-sync suppliers  then  p2p-sync invoices")
        print("Ctrl+C stops the demo.", flush=True)  # show up straight away in docker logs too

        stop = stop or threading.Event()
        with stop_on_signals(stop):
            # wait in short slices: on Windows a bare wait() can't be interrupted by Ctrl+C
            while not stop.wait(0.5):
                pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m p2p_sync.demo", description="Run the Acme demo.")
    parser.add_argument("--folder", default=".", help="where to write .env and maintenance.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--procurement-port", type=int, default=8001)
    parser.add_argument("--erp-port", type=int, default=8002)
    return run(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
