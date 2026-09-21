"""Official e-invoice validation: EN 16931, Peppol BIS Billing 3.0, XRechnung
and Factur-X / ZUGFeRD, with the published XML Schemas and Schematron.

Needs `pip install "docket-idp[einvoice]"`; see validator.py and artifacts.py.
"""
from .artifacts import ArtifactError
from .validator import (
    PROFILE_IDS,
    EInvoiceIssue,
    EInvoiceUnavailable,
    EInvoiceValidationOptions,
    EInvoiceValidationResult,
    LayerReport,
    Profile,
    available,
    validate_einvoice,
)

__all__ = [
    "ArtifactError",
    "EInvoiceIssue",
    "EInvoiceUnavailable",
    "EInvoiceValidationOptions",
    "EInvoiceValidationResult",
    "LayerReport",
    "PROFILE_IDS",
    "Profile",
    "available",
    "validate_einvoice",
]
