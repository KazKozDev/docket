"""Use docket as a library inside your own application.

    pip install docket-idp
    python examples/extract_invoice.py path/to/invoice.pdf
"""
import sys

from docket import Invoice, ProcessOptions, ReviewOptions, process_document

# Your app owns the review flow, so nothing goes to docket's review queue.
result = process_document(sys.argv[1], ProcessOptions(review=ReviewOptions(enqueue=False)))

print("type:      ", result.document_type, f"({result.status.value})")
print("valid:     ", result.is_valid)
print("review:    ", result.needs_review, result.review_reasons)

invoice = result.document
if isinstance(invoice, Invoice):
    print("number:    ", invoice.invoice_number)
    print("vendor:    ", invoice.vendor_name, invoice.vendor_vat_number)
    print("total:     ", invoice.total_amount, invoice.currency)
    # Where each value came from: the quote, and its box on the page (0..1,
    # top-left origin) — draw it over the page image next to the field in your UI.
    for field, loc in result.field_sources.items():
        where = f"page {loc.page}"
        if loc.bbox is not None:
            where += f" at ({loc.bbox.x0:.3f}, {loc.bbox.y0:.3f})–({loc.bbox.x1:.3f}, {loc.bbox.y1:.3f})"
        print(f"  {field:16} {where}  {loc.quote!r}")

for issue in result.validation_issues:
    print(f"[{issue.severity}] {issue.field}: {issue.message}")
