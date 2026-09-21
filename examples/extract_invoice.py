"""Use docket as a library inside your own application.

    pip install docket-idp
    python examples/extract_invoice.py path/to/invoice.pdf
"""
import sys

from docket import Invoice, process

result = process(sys.argv[1], enqueue_review=False)  # your app owns the review flow

print("type:      ", result.classification.doc_type.value)
print("valid:     ", result.is_valid)
print("review:    ", result.needs_review, result.review_reasons)

invoice = result.document
if isinstance(invoice, Invoice):
    print("number:    ", invoice.invoice_number)
    print("vendor:    ", invoice.vendor_name, invoice.vendor_vat_number)
    print("total:     ", invoice.total_amount, invoice.currency)
    # Where each value came from on the page — show it next to the field in your UI.
    for field, loc in result.field_sources.items():
        print(f"  {field} <- {loc}")

for issue in result.validation_issues:
    print(f"[{issue.severity}] {issue.field}: {issue.message}")
