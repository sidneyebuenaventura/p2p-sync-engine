"""Procurement system: where suppliers are onboarded and invoices get approved."""

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
    "IntegrationStatus",
    "Invoice",
    "InvoiceLine",
    "InvoiceStatus",
    "Payment",
    "Supplier",
    "SupplierRef",
    "SupplierStatus",
]
