"""Convert a PDF or scanned invoice into an EU e-invoice.

    pip install docket-idp
    python examples/export_einvoice.py invoice.pdf xrechnung > invoice.xml

Formats: xrechnung, zugferd (Factur-X / EN 16931), ubl (Peppol BIS), facturae.
Run `docket formats` for everything available.
"""
import sys

from docket import ExportError, ProcessOptions, ReviewOptions, export_document, process_document

path, fmt = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "xrechnung"
result = process_document(path, ProcessOptions(document_type="invoice", review=ReviewOptions(enqueue=False)))

try:
    # Refuses a result that failed validation or needs review: never emit a
    # legally binding e-invoice from unchecked data.
    print(export_document(result, fmt).content)
except ExportError as exc:
    sys.exit(str(exc))
