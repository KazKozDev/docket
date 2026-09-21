"""Convert a PDF or scanned invoice into an EU e-invoice.

    pip install docket-idp
    python examples/export_einvoice.py invoice.pdf xrechnung > invoice.xml

Formats: xrechnung, zugferd (Factur-X / EN 16931), ubl (Peppol BIS), facturae.
Run `docket --list-formats` for everything available.
"""
import sys

from docket import ExportError, export_document, process_document

path, fmt = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "xrechnung"
result = process_document(path, enqueue_review=False)

if not result.is_valid or result.document is None:
    # Never emit a legally binding e-invoice from data that failed validation.
    sys.exit(f"not exporting: {[i.message for i in result.validation_issues]}")

try:
    print(export_document(result.document, fmt))
except ExportError as exc:
    sys.exit(str(exc))
