"""Generic procure-to-pay sync engine for procurement, ERP and maintenance systems."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("p2p-sync-engine")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
