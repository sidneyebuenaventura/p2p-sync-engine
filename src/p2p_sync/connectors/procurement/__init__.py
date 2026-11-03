"""Procurement system: where suppliers are onboarded and invoices get approved."""

from p2p_sync.connectors.procurement.auth import ClientCredentialsAuth
from p2p_sync.connectors.procurement.client import ProcurementClient
from p2p_sync.connectors.procurement.models import (
    Address,
    BankAccount,
    IntegrationStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    Supplier,
    SupplierRef,
    SupplierStatus,
)

__all__ = [
    "Address",
    "BankAccount",
    "ClientCredentialsAuth",
    "IntegrationStatus",
    "Invoice",
    "InvoiceLine",
    "InvoiceStatus",
    "Payment",
    "ProcurementClient",
    "Supplier",
    "SupplierRef",
    "SupplierStatus",
]
