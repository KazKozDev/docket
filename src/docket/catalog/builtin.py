"""The built-in schema catalog: fourteen document types, registered at import.

Each entry names its model, version, keywords for the rules tier (the
document's own name in the main EU languages at weight 3, supporting phrases
at 1–2), sample sentences for the TF-IDF tier, the fields that must carry a
source citation, and its business rules.

Versioning: invoice-family schemas are 2.0 — parties, addresses, tax
identifiers and references became shared structured objects. The other
original schemas are 1.1 — same layout as 1.0 minus the redundant `doc_type`
field. Schemas added with the catalog start at 1.0 (experimental until
measured on real documents). Every migration from 1.0 is automatic.
"""
from __future__ import annotations

from typing import cast

from .corpus import CORPUS
from .models import (
    BankStatement,
    Contract,
    CreditNote,
    Invoice,
    PurchaseOrder,
    Receipt,
    Waybill,
)
from .registry import (
    LineItems,
    Migration,
    SchemaSpec,
    Validator,
    _register,
    keywords,
    pattern,
)
from .rules import (
    validate_credit_note,
)


def _names(regex: str) -> tuple:
    """The document's own name in several languages, one weight-3 cue."""
    return (pattern(rf"\b(?:{regex})\b", 3.0),)


# ---- migrations ---------------------------------------------------------------


def _drop_doc_type(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "doc_type"}


def _flat_party(data: dict, name: str, address: str | None = None, *, vat: str | None = None,
                tax: str | None = None) -> dict:
    party: dict = {"name": data.get(name) or ""}
    if address and data.get(address):
        party["address"] = {"text": data[address]}
    tax_ids = []
    if vat and data.get(vat):
        tax_ids.append({"value": data[vat], "scheme": "vat"})
    if tax and data.get(tax):
        tax_ids.append({"value": data[tax], "scheme": "tax_id"})
    if tax_ids:
        party["tax_ids"] = tax_ids
    return party


def _rename_citations(data: dict, renames: dict[str, str]) -> dict:
    locations = data.get("field_locations") or {}
    return {renames.get(k, k): v for k, v in locations.items()}


def _upgrade_invoice(data: dict) -> dict:
    """1.0 flat vendor_*/customer_* fields → 2.0 seller/buyer parties."""
    out = {
        k: v
        for k, v in data.items()
        if not k.startswith(("vendor_", "customer_")) and k not in {"doc_type", "purchase_order_number"}
    }
    out["seller"] = _flat_party(data, "vendor_name", "vendor_address", vat="vendor_vat_number", tax="vendor_tax_id")
    out["buyer"] = _flat_party(data, "customer_name", "customer_address", tax="customer_tax_id")
    if data.get("vendor_iban") or data.get("vendor_bic"):
        out["payment_account"] = {"iban": data.get("vendor_iban"), "bic": data.get("vendor_bic")}
    if data.get("purchase_order_number"):
        out["references"] = [{"kind": "purchase_order", "number": data["purchase_order_number"]}]
    vat_index = 0 if data.get("vendor_vat_number") else None
    tax_index = (1 if vat_index == 0 else 0) if data.get("vendor_tax_id") else None
    renames = {"vendor_name": "seller.name", "customer_name": "buyer.name",
               "vendor_address": "seller.address", "customer_address": "buyer.address",
               "customer_tax_id": "buyer.tax_ids[0]", "vendor_iban": "payment_account.iban",
               "vendor_bic": "payment_account.bic", "purchase_order_number": "references[0].number"}
    if vat_index is not None:
        renames["vendor_vat_number"] = f"seller.tax_ids[{vat_index}]"
    if tax_index is not None:
        renames["vendor_tax_id"] = f"seller.tax_ids[{tax_index}]"
    if "field_locations" in data:
        out["field_locations"] = _rename_citations(data, renames)
    return out


def _upgrade_purchase_order(data: dict) -> dict:
    """1.0 vendor_name/customer_name → 2.0 supplier/buyer parties."""
    out = {k: v for k, v in data.items() if k not in {"doc_type", "vendor_name", "customer_name"}}
    out["supplier"] = {"name": data.get("vendor_name") or ""}
    out["buyer"] = {"name": data.get("customer_name") or ""}
    if "field_locations" in data:
        out["field_locations"] = _rename_citations(
            data, {"vendor_name": "supplier.name", "customer_name": "buyer.name"}
        )
    return out


_FLAT_MIGRATION = (
    Migration("1.0", "1.1", "drops the doc_type field; the result's document_type carries it", _drop_doc_type),
)
_RECEIPT_MIGRATION = _FLAT_MIGRATION + (
    Migration("1.1", "1.2", "adds the optional amount_tendered and change_given fields", lambda data: dict(data)),
)
_INVOICE_MIGRATION = (
    Migration(
        "1.0",
        "2.0",
        "vendor_*/customer_* fields become seller/buyer Party objects (address, tax_ids), "
        "vendor_iban/vendor_bic become payment_account, purchase_order_number becomes a reference; "
        "doc_type is dropped",
        _upgrade_invoice,
    ),
)

_BILL_ITEMS = LineItems("line_items", {c: c for c in ("description", "sku", "quantity", "unit_of_measure", "unit_price", "total", "tax_rate_percent")})
_BILLING_SUMMARY = {
    "document_number": "invoice_number",
    "document_date": "issue_date",
    "issuer": "seller.name",
    "recipient": "buyer.name",
    "currency": "currency",
    "subtotal": "subtotal",
    "tax_amount": "tax_amount",
    "total_amount": "total_amount",
}

_BILLING_CITED = ("invoice_number", "issue_date", "seller.name", "buyer.name", "subtotal", "total_amount")


BUILTIN_SCHEMAS: tuple[SchemaSpec, ...] = (
    SchemaSpec(
        schema_id="invoice",
        version="2.0",
        display_name="Invoice",
        model=Invoice,
        description="Invoice / factura / Rechnung requesting payment for goods or services",
        keywords=(
            # A bare TAX INVOICE / factura fiscal header is not invoice evidence: in
            # GST countries every till receipt prints one. It scores nothing, so
            # such a document goes to the next tier, whose receipt description
            # names that case (82/120 Malaysian SROIE receipts were misfiled
            # before, first as invoices, then under a separate tax_invoice type).
            pattern(r"\b(?<!tax\s)invoice\b|\bfactura\b(?!\s*fiscal)", 3.0),
            pattern(r"\bbill to\b|\bfacturar a\b|\bcliente\b", 2.0),
            pattern(r"\bamount due\b|\bimporte total\b|\btotal a pagar\b", 2.0),
            pattern(r"\bdue date\b|\bfecha de vencimiento\b|\bvencimiento\b", 1.0),
            pattern(r"\bpo number\b|\bpurchase order\b|\bpedido\b", 1.0),
            pattern(r"\bbase imponible\b|\bn[úu]mero de factura\b", 2.0),
        ) + _names(
            r"rechnung|rechnungsnummer|facture(?!\s*fiscale)|fattura(?!\s*fiscale)|"
            r"factuur|fatura(?!\s*fiscal)|faktura(?!\s+vat\b)"
        ),
        summary={"document_number": "invoice_number", "document_date": "issue_date", "issuer": "seller.name", "recipient": "buyer.name", "currency": "currency", "subtotal": "subtotal", "tax_amount": "tax_amount", "total_amount": "total_amount"},
        line_items=_BILL_ITEMS,
        cited_fields=_BILLING_CITED,
        migrations=_INVOICE_MIGRATION,
    ),
    SchemaSpec(
        schema_id="credit_note",
        version="1.0",
        display_name="Credit note",
        status="experimental",
        model=CreditNote,
        description="Credit note / Gutschrift / avoir crediting part or all of an earlier invoice",
        # Weighted above the invoice cues a credit note always repeats
        # ("original invoice INV-…").
        keywords=keywords(
            "credit note", "credit memo", "nota de crédito", "nota de credito", "factura rectificativa", "gutschrift",
            "rechnungskorrektur", "avoir", "facture d'avoir", "nota di credito", "creditnota", "creditfactuur",
            "faktura korygująca", "корректировочный счет", weight=6.0,
        ),
        summary={**_BILLING_SUMMARY, "document_number": "credit_note_number"},
        line_items=_BILL_ITEMS,
        cited_fields=("credit_note_number", "issue_date", "seller.name", "buyer.name", "total_amount"),
    ),
    SchemaSpec(
        schema_id="receipt",
        version="1.2",
        display_name="Receipt",
        model=Receipt,
        description=(
            "Receipt or till slip proving a payment was made — including point-of-sale "
            "till receipts printed with a TAX INVOICE header (cashier, salesperson, "
            "approval code, goods sold are not returnable)"
        ),
        keywords=(
            pattern(r"\breceipt\b|\brecibo\b|\btique\b|\bticket de compra\b", 3.0),
            pattern(r"\bthank you for your purchase\b|\bgracias por su compra\b", 2.0),
            pattern(r"\bchange due\b|\bcambio\b|\bentregado\b", 2.0),
            pattern(r"\bcashier\b|\bcajero?a?\b", 1.0),
            pattern(r"\btender(ed)?\b|\befectivo\b", 1.0),
            # Till/terminal signals: a cash-register slip that prints its own legal
            # header (Malaysian "TAX INVOICE") is still a receipt, not a B2B invoice.
            # The cashier+approval-code combination is the point-of-sale signature;
            # no single phrase outranks a printed TAX INVOICE header on its own.
            pattern(r"\bcashier\b[\s\S]{0,300}\bapproval code\b|\bapproval code\b[\s\S]{0,300}\bcashier\b", 3.0),
            pattern(r"\bplease come again\b", 2.0),
            pattern(r"\bapproval code\b", 1.0),
            pattern(r"\bterima kasih\b", 2.0),
            # Every till slip in a GST country prints TAX INVOICE, so the header
            # is weak receipt evidence. One point keeps it from deciding alone
            # and stops a lone "Bill To" beside it from making an invoice.
            pattern(r"\btax invoice\b", 1.0),
        ) + _names(
            r"kassenbon|kassenbeleg|quittung|ticket de caisse|re[çc]u|scontrino|"
            r"ricevuta|kassabon|kassabonnetje|tal[ãa]o|paragon"
        ),
        summary={"document_number": "receipt_number", "document_date": "transaction_date", "issuer": "merchant_name", "currency": "currency", "subtotal": "subtotal", "tax_amount": "tax_amount", "total_amount": "total_amount"},
        line_items=LineItems("items", {"description": "description", "quantity": "quantity", "unit_price": "unit_price", "total": "price"}),
        cited_fields=("merchant_name", "transaction_date", "total_amount"),
        migrations=_RECEIPT_MIGRATION,
    ),
    SchemaSpec(
        schema_id="contract",
        version="1.1",
        display_name="Contract",
        model=Contract,
        description="Contract or agreement between parties",
        keywords=(
            pattern(r"\bagreement\b|\bcontrato\b|\bacuerdo\b", 3.0),
            pattern(r"\bwhereas\b|\bexponen\b|\bmanifiestan\b", 2.0),
            pattern(r"\bhereby agrees?\b|\bacuerdan\b|\bcl[áa]usulas\b", 2.0),
            pattern(r"\bgoverning law\b|\blegislaci[óo]n aplicable\b|\bley aplicable\b", 2.0),
            pattern(r"\bparty of the first part\b|\bthe parties\b|\blas partes\b|\bde una parte\b", 1.0),
        ) + _names(r"vertrag|vereinbarung|contrat|contratto|overeenkomst|umowa"),
        summary={"document_number": "contract_title", "document_date": "effective_date", "issuer": "parties_a[0]", "recipient": "parties_b[0]", "currency": "currency", "total_amount": "contract_value"},
        cited_fields=("contract_title", "parties_a", "parties_b", "effective_date"),
        migrations=_FLAT_MIGRATION,
    ),
    SchemaSpec(
        schema_id="purchase_order",
        version="2.0",
        display_name="Purchase order",
        model=PurchaseOrder,
        description="Purchase order issued by a buyer to a supplier",
        keywords=(
            pattern(r"\bpurchase order\b|\border confirmation\b|\borden de compra\b", 3.0),
            pattern(r"\bpo number\b|\bn[úu]mero de pedido\b|\bpo #\b", 2.0),
            pattern(r"\bvendor\b|\bproveedor\b|\bship to\b|\bentregar en\b", 2.0),
            pattern(r"\brequisition\b|\border date\b|\bfecha de pedido\b", 1.0),
        ) + _names(
            r"bestellung|bon de commande|ordine d'acquisto|ordine di acquisto|"
            r"inkooporder|bestelbon|nota de encomenda|zam[óo]wienie"
        ),
        summary={"document_number": "po_number", "document_date": "po_date", "issuer": "buyer.name", "recipient": "supplier.name", "currency": "currency", "subtotal": "subtotal", "tax_amount": "tax_amount", "total_amount": "total_amount"},
        line_items=_BILL_ITEMS,
        cited_fields=("po_number", "po_date", "buyer.name", "supplier.name", "total_amount"),
        migrations=(
            Migration(
                "1.0", "2.0",
                "vendor_name/customer_name become supplier/buyer Party objects; doc_type is dropped",
                _upgrade_purchase_order,
            ),
        ),
    ),
    SchemaSpec(
        schema_id="bank_statement",
        version="1.1",
        display_name="Bank statement",
        model=BankStatement,
        description="Bank account statement listing transactions",
        keywords=(
            pattern(r"\bbank statement\b|\baccount statement\b|\bextracto bancario\b|\bвыписка\b", 3.0),
            pattern(r"\bopening balance\b|\bclosing balance\b|\bsaldo inicial\b|\bsaldo final\b", 2.0),
            pattern(r"\bdeposits?\b|\bwithdrawals?\b|\bmovimientos?\b|\btransacciones\b", 2.0),
            pattern(r"\bstatement period\b|\bper[íi]odo del extracto\b|\baccount number\b", 1.0),
        ) + _names(
            r"kontoauszug|relev[ée] de compte|relev[ée] bancaire|estratto conto|"
            r"rekeningafschrift|extrato banc[áa]rio|wyci[ąa]g bankowy"
        ),
        summary={"document_date": "statement_period_end", "issuer": "bank_name", "recipient": "account_holder", "currency": "currency", "total_amount": "closing_balance"},
        line_items=LineItems("transactions", {"description": "description", "total": "amount"}),
        cited_fields=("bank_name", "account_holder", "closing_balance"),
        migrations=_FLAT_MIGRATION,
    ),
    SchemaSpec(
        schema_id="waybill",
        version="1.1",
        display_name="Waybill",
        model=Waybill,
        description="Waybill, consignment note or bill of lading for shipped goods",
        keywords=(
            pattern(
                r"\bwaybill\b|\bbill of lading\b|\bconsignment note\b|\bcmr\b|\bтоварная накладная\b|"
                r"\bторг-12\b|\balbar[áa]n\b",
                3.0,
            ),
            pattern(
                r"\bconsignee\b|\bconsignor\b|\bshipper\b|\bdestinatario\b|\bremitente\b|\bгрузополучатель\b", 2.0
            ),
            pattern(r"\bgross weight\b|\bnet weight\b|\bpeso bruto\b|\bpeso neto\b|\bвес брутто\b", 2.0),
            pattern(r"\bcarrier\b|\btransportista\b|\bcarrier tracking\b|\bvehicle\b|\bveh[íi]culo\b", 1.0),
        ) + _names(
            r"frachtbrief|lettre de voiture|documento di trasporto|vrachtbrief|guia de transporte|list przewozowy"
        ),
        summary={"document_number": "waybill_number", "document_date": "waybill_date", "issuer": "shipper_name", "recipient": "consignee_name", "currency": "currency", "total_amount": "total_amount"},
        line_items=LineItems("items", {"description": "item_name", "sku": "sku", "quantity": "quantity", "unit_of_measure": "unit_of_measure", "unit_price": "unit_price", "total": "total_price"}),
        cited_fields=("shipper_name", "consignee_name", "waybill_number"),
        migrations=_FLAT_MIGRATION,
    ),
)


def register_builtins() -> None:
    from dataclasses import replace

    from ..validate import (
        validate_bank_statement,
        validate_billing,
        validate_contract,
        validate_purchase_order,
        validate_receipt,
        validate_waybill,
    )

    rules = {
        "invoice": (validate_billing,),
        "credit_note": (validate_billing, validate_credit_note),
        "receipt": (validate_receipt,),
        "contract": (validate_contract,),
        "purchase_order": (validate_purchase_order,),
        "bank_statement": (validate_bank_statement,),
        "waybill": (validate_waybill,),
    }
    for spec in BUILTIN_SCHEMAS:
        _register(
            replace(
                spec,
                # Each validator takes its own schema; the registry pairs them.
                validators=cast("tuple[Validator, ...]", rules[spec.schema_id]),
                examples=tuple(CORPUS[spec.schema_id]),
                builtin=True,
            )
        )


__all__ = ["BUILTIN_SCHEMAS", "register_builtins"]
