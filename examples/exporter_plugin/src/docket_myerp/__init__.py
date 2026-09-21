"""Example docket exporter plugin, loaded through the `docket.exporters` entry point."""
from docket import Invoice, register_exporter


def to_myerp(invoice: Invoice) -> dict:
    # Return a dict and docket renders it as JSON; return a str for any other format.
    return {
        "documentNo": invoice.invoice_number,
        "supplier": {"name": invoice.vendor_name, "vatId": invoice.vendor_vat_number},
        "amount": {"gross": invoice.total_amount, "currency": invoice.currency},
    }


def register() -> None:
    register_exporter("myerp-json", to_myerp, accepts=(Invoice,), description="MyERP bill JSON")
