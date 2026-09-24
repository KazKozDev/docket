"""Convert a PDF or scanned invoice into an EU e-invoice and check it with the
official rules before you send it.

    pip install "docket-idp[einvoice]"
    python examples/export_einvoice.py invoice.pdf xrechnung-ubl > invoice.xml

Formats: ubl, peppol, xrechnung-ubl, xrechnung-cii, factur-x-en16931,
factur-x-basic (and facturae, which has no official validator here).
Run `docket formats` for everything available.
"""
import sys

from docket import (
    ExportError,
    ExportOptions,
    ProcessOptions,
    ReviewOptions,
    export_document,
    process_document,
)

path, fmt = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "xrechnung-ubl"
result = process_document(path, ProcessOptions(document_type="invoice", review=ReviewOptions(enqueue=False)))

try:
    # Refuses a result that failed validation or needs review, and an invoice
    # the format can't represent faithfully (no lines, unclear tax rate, ...):
    # never emit a legally binding e-invoice from unchecked data.
    exported = export_document(result, fmt, ExportOptions(validate_einvoice=fmt != "facturae"))
except ExportError as exc:
    sys.exit(str(exc))

report = exported.einvoice_validation
if report is not None and not report.valid:
    for issue in report.issues:
        print(f"{issue.severity} {issue.code}: {issue.message}", file=sys.stderr)
    sys.exit(f"{fmt} output breaks the official rules ({report.validation_resource_version})")
print(exported.content)
