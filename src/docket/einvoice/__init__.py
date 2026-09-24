"""Official e-invoice validation: EN 16931, Peppol BIS Billing 3.0, XRechnung
and Factur-X / ZUGFeRD, with the published XML Schemas and Schematron.

Needs `pip install "docket-idp[einvoice]"`; see validator.py and artifacts.py.
Peppol, CII and Factur-X validation also need `fetch_einvoice_resources()`
once (fetch.py).
"""
from .artifacts import ArtifactError
from .facturx_pdf import (
    FacturXPdfUnavailable,
    FacturXRoundTripResult,
    PdfAValidationIssue,
    PdfAValidationResult,
    extract_facturx_xml,
    generate_facturx_pdf,
    validate_pdfa,
    verify_facturx_round_trip,
)
from .fetch import fetch_einvoice_resources
from .validator import (
    PROFILE_IDS,
    EInvoiceIssue,
    EInvoiceResourcesMissing,
    EInvoiceUnavailable,
    EInvoiceValidationOptions,
    EInvoiceValidationResult,
    LayerReport,
    Profile,
    available,
    validate_einvoice,
)

__all__ = [
    "PROFILE_IDS",
    "ArtifactError",
    "EInvoiceIssue",
    "EInvoiceResourcesMissing",
    "EInvoiceUnavailable",
    "EInvoiceValidationOptions",
    "EInvoiceValidationResult",
    "FacturXPdfUnavailable",
    "FacturXRoundTripResult",
    "LayerReport",
    "PdfAValidationIssue",
    "PdfAValidationResult",
    "Profile",
    "available",
    "extract_facturx_xml",
    "fetch_einvoice_resources",
    "generate_facturx_pdf",
    "validate_einvoice",
    "validate_pdfa",
    "verify_facturx_round_trip",
]
