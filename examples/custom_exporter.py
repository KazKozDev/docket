"""Add your own output format at runtime.

After registration it works everywhere docket resolves formats by name:
`export_document(doc, "my-erp-csv")` and `docket file.pdf --export my-erp-csv`
(the latter only if registered via a plugin package, see exporter_plugin/).
"""
import csv
import io
import sys

from docket import Invoice, export_document, process_document, register_exporter


def to_my_erp_csv(invoice: Invoice) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["number", "date", "vendor", "vat_id", "net", "total", "currency"])
    writer.writerow([
        invoice.invoice_number,
        invoice.issue_date.isoformat(),
        invoice.vendor_name,
        invoice.vendor_vat_number or "",
        invoice.subtotal,
        invoice.total_amount,
        invoice.currency,
    ])
    return buf.getvalue()


register_exporter(
    "my-erp-csv", to_my_erp_csv, accepts=(Invoice,), description="Our ERP's import CSV"
)

if __name__ == "__main__":
    result = process_document(sys.argv[1], enqueue_review=False)
    print(export_document(result.document, "my-erp-csv"))
