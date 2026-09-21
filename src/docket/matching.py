"""Deterministic cross-document matching and reconciliation.

Provides:
- match_invoice_to_po: 2-way / 3-way matching between Invoices and Purchase Orders.
- match_invoices_to_contract: Budget and compliance auditing of Invoices against Contracts.
- match_receipt_to_transactions: Reconciling Receipts against Bank Transactions.
"""
from __future__ import annotations

from typing import Sequence

from .catalog.common import LineItem
from .catalog.models import Contract, Invoice, PurchaseOrder, Receipt, Waybill, WaybillItem
from .schemas import BankTransaction, Discrepancy, DiscrepancyType, MatchingStatus, MatchResult
from .validate import _core_name, _isclose


def match_invoice_to_po(
    invoice: Invoice,
    po: PurchaseOrder,
    *,
    price_tolerance_pct: float = 0.0,
    qty_tolerance_pct: float = 0.0,
) -> MatchResult:
    """Reconcile an invoice against a purchase order (PO matching).

    Checks:
    1. PO Reference: verifies invoice.purchase_order_number matches po.po_number.
    2. Currency: verifies currencies match.
    3. Vendor & Customer: verifies counterparties match.
    4. Line Items: matches line items by SKU or description, detecting price variances,
       quantity over-billing, and unordered items.
    5. Totals: cross-checks total amounts.

    Args:
        invoice: Extracted invoice.
        po: Authorized purchase order.
        price_tolerance_pct: Allowable price increase percentage (default 0.0%).
        qty_tolerance_pct: Allowable quantity increase percentage (default 0.0%).

    Returns:
        MatchResult with status, matched amounts, and any detected discrepancies.
    """
    discrepancies: list[Discrepancy] = []

    # 1. PO Number Check
    if invoice.purchase_order_number:
        inv_po = invoice.purchase_order_number.strip().lower()
        exp_po = po.po_number.strip().lower()
        if inv_po != exp_po:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.PO_NUMBER_MISMATCH,
                    field="purchase_order_number",
                    expected=po.po_number,
                    actual=invoice.purchase_order_number,
                    severity="error",
                    message=(
                        f"Invoice PO reference '{invoice.purchase_order_number}' "
                        f"does not match PO '{po.po_number}'"
                    ),
                )
            )

    # 2. Currency Check
    if invoice.currency.upper() != po.currency.upper():
        discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.CURRENCY_MISMATCH,
                field="currency",
                expected=po.currency,
                actual=invoice.currency,
                severity="error",
                message=f"Currency mismatch: invoice is {invoice.currency}, PO is {po.currency}",
            )
        )

    # 3. Vendor and Customer Check
    if _core_name(invoice.seller.name) != _core_name(po.supplier.name):
        discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.PARTY_MISMATCH,
                field="seller.name",
                expected=po.supplier.name,
                actual=invoice.seller.name,
                severity="error",
                message=(
                    f"Vendor mismatch: invoice vendor '{invoice.seller.name}' "
                    f"does not match PO vendor '{po.supplier.name}'"
                ),
            )
        )

    if _core_name(invoice.buyer.name) != _core_name(po.buyer.name):
        discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.PARTY_MISMATCH,
                field="buyer.name",
                expected=po.buyer.name,
                actual=invoice.buyer.name,
                severity="error",
                message=(
                    f"Customer mismatch: invoice customer '{invoice.buyer.name}' "
                    f"does not match PO customer '{po.buyer.name}'"
                ),
            )
        )

    # 4. Line Items Check
    po_items_by_sku: dict[str, LineItem] = {}
    po_items_by_desc: dict[str, LineItem] = {}
    for item in po.line_items:
        if item.sku:
            po_items_by_sku[item.sku.strip().lower()] = item
        po_items_by_desc[item.description.strip().lower()] = item

    for idx, inv_item in enumerate(invoice.line_items):
        po_match: LineItem | None = None
        if inv_item.sku and inv_item.sku.strip().lower() in po_items_by_sku:
            po_match = po_items_by_sku[inv_item.sku.strip().lower()]
        elif inv_item.description.strip().lower() in po_items_by_desc:
            po_match = po_items_by_desc[inv_item.description.strip().lower()]

        if po_match is None:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.UNORDERED_ITEM,
                    field=f"line_items[{idx}]",
                    expected=None,
                    actual=inv_item.description,
                    severity="error",
                    message=(
                        f"Unordered item on invoice: '{inv_item.description}' "
                        f"(SKU: {inv_item.sku or 'N/A'}) was not in PO {po.po_number}"
                    ),
                )
            )
            continue

        # Quantity Check
        max_allowed_qty = po_match.quantity * (1.0 + qty_tolerance_pct / 100.0)
        if inv_item.quantity > max_allowed_qty and not _isclose(
            inv_item.quantity, po_match.quantity
        ):
            diff = round(inv_item.quantity - po_match.quantity, 2)
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.QUANTITY_OVERBILLING,
                    field=f"line_items[{idx}].quantity",
                    expected=po_match.quantity,
                    actual=inv_item.quantity,
                    difference=diff,
                    severity="error",
                    message=(
                        f"Over-billing on '{inv_item.description}': invoiced quantity {inv_item.quantity} "
                        f"exceeds PO authorized quantity {po_match.quantity}"
                    ),
                )
            )

        # Price Check
        max_allowed_price = po_match.unit_price * (1.0 + price_tolerance_pct / 100.0)
        if inv_item.unit_price > max_allowed_price and not _isclose(
            inv_item.unit_price, po_match.unit_price
        ):
            diff = round(inv_item.unit_price - po_match.unit_price, 2)
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.PRICE_VARIANCE,
                    field=f"line_items[{idx}].unit_price",
                    expected=po_match.unit_price,
                    actual=inv_item.unit_price,
                    difference=diff,
                    severity="error",
                    message=(
                        f"Price variance on '{inv_item.description}': invoiced unit price {inv_item.unit_price:.2f} "
                        f"exceeds PO unit price {po_match.unit_price:.2f}"
                    ),
                )
            )

    # 5. Total Amount Check
    if invoice.total_amount > po.total_amount and not _isclose(
        invoice.total_amount, po.total_amount
    ):
        diff = round(invoice.total_amount - po.total_amount, 2)
        discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.TOTAL_MISMATCH,
                field="total_amount",
                expected=po.total_amount,
                actual=invoice.total_amount,
                difference=diff,
                severity="error",
                message=(
                    f"Invoice total {invoice.total_amount:.2f} exceeds PO total {po.total_amount:.2f} "
                    f"by {diff:.2f}"
                ),
            )
        )

    matched_amount = min(invoice.total_amount, po.total_amount)
    variance_amount = round(abs(invoice.total_amount - po.total_amount), 2)
    has_errors = any(d.severity == "error" for d in discrepancies)
    status = MatchingStatus.DISCREPANCY if has_errors else MatchingStatus.MATCHED

    return MatchResult(
        status=status,
        matched_amount=matched_amount,
        variance_amount=variance_amount,
        discrepancies=discrepancies,
        metadata={
            "invoice_number": invoice.invoice_number,
            "po_number": po.po_number,
            "items_checked": len(invoice.line_items),
        },
    )


def match_invoices_to_contract(
    contract: Contract,
    invoices: Sequence[Invoice],
) -> MatchResult:
    """Reconcile a set of invoices against an active contract.

    Audits:
    1. Party Affiliation: verifies invoice parties belong to contract parties.
    2. Date Validity: verifies invoice issue_date falls within the contract term.
    3. Currency Consistency: verifies invoice currency matches contract currency.
    4. Cumulative Budget Ceiling: verifies total invoiced amount does not exceed contract_value.

    Args:
        contract: Governing commercial contract.
        invoices: Sequence of invoices charged against this contract.

    Returns:
        MatchResult with compliance status and details of any breaches.
    """
    discrepancies: list[Discrepancy] = []
    contract_parties = {_core_name(p) for p in contract.parties_a} | {
        _core_name(p) for p in contract.parties_b
    }

    total_billed = 0.0

    for inv in invoices:
        total_billed += inv.total_amount

        # 1. Party Check
        v_core = _core_name(inv.seller.name)
        c_core = _core_name(inv.buyer.name)
        if v_core not in contract_parties:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.PARTY_MISMATCH,
                    field=f"invoices[{inv.invoice_number}].seller.name",
                    expected=contract.parties_a,
                    actual=inv.seller.name,
                    severity="error",
                    message=(
                        f"Invoice {inv.invoice_number} vendor '{inv.seller.name}' "
                        f"is not a party to contract '{contract.contract_title}'"
                    ),
                )
            )
        if c_core not in contract_parties:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.PARTY_MISMATCH,
                    field=f"invoices[{inv.invoice_number}].buyer.name",
                    expected=contract.parties_b,
                    actual=inv.buyer.name,
                    severity="error",
                    message=(
                        f"Invoice {inv.invoice_number} customer '{inv.buyer.name}' "
                        f"is not a party to contract '{contract.contract_title}'"
                    ),
                )
            )

        # 2. Date Bounds Check
        if inv.issue_date < contract.effective_date:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.DATE_OUT_OF_BOUNDS,
                    field=f"invoices[{inv.invoice_number}].issue_date",
                    expected=contract.effective_date,
                    actual=inv.issue_date,
                    severity="error",
                    message=(
                        f"Invoice {inv.invoice_number} issue date {inv.issue_date} "
                        f"is prior to contract effective date {contract.effective_date}"
                    ),
                )
            )
        if contract.expiration_date and inv.issue_date > contract.expiration_date:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.DATE_OUT_OF_BOUNDS,
                    field=f"invoices[{inv.invoice_number}].issue_date",
                    expected=contract.expiration_date,
                    actual=inv.issue_date,
                    severity="error",
                    message=(
                        f"Invoice {inv.invoice_number} issue date {inv.issue_date} "
                        f"is after contract expiration date {contract.expiration_date}"
                    ),
                )
            )

        # 3. Currency Check
        if contract.currency and inv.currency.upper() != contract.currency.upper():
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.CURRENCY_MISMATCH,
                    field=f"invoices[{inv.invoice_number}].currency",
                    expected=contract.currency,
                    actual=inv.currency,
                    severity="error",
                    message=(
                        f"Invoice {inv.invoice_number} currency {inv.currency} "
                        f"does not match contract currency {contract.currency}"
                    ),
                )
            )

    # 4. Cumulative Budget Ceiling Check
    variance = 0.0
    matched_amount = total_billed
    if contract.contract_value is not None:
        if total_billed > contract.contract_value and not _isclose(
            total_billed, contract.contract_value
        ):
            overage = round(total_billed - contract.contract_value, 2)
            variance = overage
            matched_amount = contract.contract_value
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.BUDGET_EXCEEDED,
                    field="contract_value",
                    expected=contract.contract_value,
                    actual=total_billed,
                    difference=overage,
                    severity="error",
                    message=(
                        f"Cumulative invoices total ({total_billed:.2f}) exceeds "
                        f"contract value limit ({contract.contract_value:.2f}) by {overage:.2f}"
                    ),
                )
            )

    has_errors = any(d.severity == "error" for d in discrepancies)
    status = MatchingStatus.DISCREPANCY if has_errors else MatchingStatus.MATCHED

    return MatchResult(
        status=status,
        matched_amount=matched_amount,
        variance_amount=variance,
        discrepancies=discrepancies,
        metadata={
            "contract_title": contract.contract_title,
            "invoices_count": len(invoices),
            "total_invoiced": total_billed,
            "contract_value": contract.contract_value,
        },
    )


def match_receipt_to_transactions(
    receipt: Receipt,
    transactions: Sequence[BankTransaction],
    *,
    date_tolerance_days: int = 3,
    amount_tolerance: float = 0.05,
) -> MatchResult:
    """Match a receipt to a list of bank/card transactions.

    Matches by:
    1. Card Number: last 4 digits (if printed on receipt and provided on transaction).
    2. Currency: matching ISO currency codes.
    3. Date Window: transaction date within +/- date_tolerance_days of receipt date.
    4. Amount: transaction amount matches receipt total_amount within amount_tolerance.

    Args:
        receipt: Physical or digital receipt.
        transactions: Bank or card transactions from account feed.
        date_tolerance_days: Max days difference to allow for settlement clearing (default 3).
        amount_tolerance: Amount floating point tolerance (default 0.05).

    Returns:
        MatchResult with MATCHED status and matched transaction info, or UNMATCHED.
    """
    candidates: list[BankTransaction] = []

    for tx in transactions:
        # Currency check
        if receipt.currency.upper() != tx.currency.upper():
            continue

        # Card check (if both have card_last_four)
        if receipt.card_last_four and tx.card_last_four:
            if receipt.card_last_four != tx.card_last_four:
                continue

        # Date tolerance check
        days_diff = abs((tx.transaction_date - receipt.transaction_date).days)
        if days_diff > date_tolerance_days:
            continue

        # Amount check (tx amount can be negative for debit or positive)
        tx_abs_amount = abs(tx.amount)
        if not _isclose(receipt.total_amount, tx_abs_amount, tol=amount_tolerance):
            continue

        candidates.append(tx)

    if len(candidates) == 1:
        matched_tx = candidates[0]
        return MatchResult(
            status=MatchingStatus.MATCHED,
            matched_amount=receipt.total_amount,
            variance_amount=0.0,
            metadata={
                "matched_transaction_id": matched_tx.transaction_id,
                "matched_transaction_date": str(matched_tx.transaction_date),
                "matched_amount": matched_tx.amount,
                "card_last_four": matched_tx.card_last_four,
            },
        )
    elif len(candidates) > 1:
        # Multiple matches: pick the one with exact same date if available, otherwise first with a warning
        exact_date = [
            c for c in candidates if c.transaction_date == receipt.transaction_date
        ]
        best_tx = exact_date[0] if exact_date else candidates[0]
        return MatchResult(
            status=MatchingStatus.MATCHED,
            matched_amount=receipt.total_amount,
            variance_amount=0.0,
            discrepancies=[
                Discrepancy(
                    type=DiscrepancyType.TOTAL_MISMATCH,
                    field="transactions",
                    expected=1,
                    actual=len(candidates),
                    severity="warning",
                    message=(
                        f"Found {len(candidates)} matching bank transactions for receipt. "
                        f"Selected closest transaction (ID: {best_tx.transaction_id or 'N/A'})."
                    ),
                )
            ],
            metadata={
                "matched_transaction_id": best_tx.transaction_id,
                "matched_transaction_date": str(best_tx.transaction_date),
                "candidates_count": len(candidates),
            },
        )

    # No match found
    return MatchResult(
        status=MatchingStatus.UNMATCHED,
        matched_amount=0.0,
        variance_amount=receipt.total_amount,
        discrepancies=[
            Discrepancy(
                type=DiscrepancyType.TOTAL_MISMATCH,
                field="transactions",
                expected=receipt.total_amount,
                actual=0.0,
                severity="error",
                message=(
                    f"No matching bank transaction found for receipt of {receipt.total_amount:.2f} "
                    f"{receipt.currency} dated {receipt.transaction_date} "
                    f"(card: {receipt.card_last_four or 'N/A'}, tolerance: +/- {date_tolerance_days} days)."
                ),
            )
        ],
        metadata={
            "transactions_searched": len(transactions),
            "receipt_merchant": receipt.merchant_name,
        },
    )


def match_three_way(
    po: PurchaseOrder,
    waybill: Waybill,
    invoice: Invoice,
    *,
    price_tolerance_pct: float = 0.0,
    qty_tolerance_pct: float = 0.0,
) -> MatchResult:
    """Perform classic 3-Way Matching: Purchase Order (PO) ↔ Goods Receipt (Waybill) ↔ Invoice.

    Audits:
    1. Party Consistency: PO vendor == Waybill shipper == Invoice vendor;
       PO customer == Waybill consignee == Invoice customer.
    2. Fulfillment (Waybill vs Invoice): Quantities billed on Invoice do not exceed
       quantities received on Waybill (UNFULFILLED_BILLING).
    3. Pricing (PO vs Invoice): Invoiced unit prices match PO authorized pricing (PRICE_VARIANCE).
    4. Ordering (PO vs Invoice): Invoice items were authorized on PO (UNORDERED_ITEM).

    Args:
        po: Authorized purchase order.
        waybill: Physical goods receipt / delivery note.
        invoice: Billed invoice.
        price_tolerance_pct: Allowable price increase percentage.
        qty_tolerance_pct: Allowable quantity increase percentage.

    Returns:
        MatchResult with 3-way matching status and details of any variance.
    """
    discrepancies: list[Discrepancy] = []

    # 1. Party Checks
    po_vendor = _core_name(po.supplier.name)
    wb_shipper = _core_name(waybill.shipper_name)
    inv_vendor = _core_name(invoice.seller.name)

    if po_vendor != wb_shipper or po_vendor != inv_vendor:
        discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.PARTY_MISMATCH,
                field="seller.name",
                expected=po.supplier.name,
                actual=f"Waybill: '{waybill.shipper_name}', Invoice: '{invoice.seller.name}'",
                severity="error",
                message=(
                    f"Vendor mismatch across 3-way match: PO is '{po.supplier.name}', "
                    f"Waybill shipper is '{waybill.shipper_name}', Invoice vendor is '{invoice.seller.name}'"
                ),
            )
        )

    po_customer = _core_name(po.buyer.name)
    wb_consignee = _core_name(waybill.consignee_name)
    inv_customer = _core_name(invoice.buyer.name)

    if po_customer != wb_consignee or po_customer != inv_customer:
        discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.PARTY_MISMATCH,
                field="buyer.name",
                expected=po.buyer.name,
                actual=f"Waybill: '{waybill.consignee_name}', Invoice: '{invoice.buyer.name}'",
                severity="error",
                message=(
                    f"Customer mismatch across 3-way match: PO is '{po.buyer.name}', "
                    f"Waybill consignee is '{waybill.consignee_name}', Invoice customer is '{invoice.buyer.name}'"
                ),
            )
        )

    # 2. Map items by SKU and description
    po_items: dict[str, LineItem] = {}
    for item in po.line_items:
        k = item.sku.strip().lower() if item.sku else item.description.strip().lower()
        po_items[k] = item

    wb_items: dict[str, WaybillItem] = {}
    for item in waybill.items:
        k = item.sku.strip().lower() if item.sku else item.item_name.strip().lower()
        wb_items[k] = item

    # 3. Check Invoice items against Waybill (fulfillment) and PO (authorization & price)
    for idx, inv_item in enumerate(invoice.line_items):
        k = (
            inv_item.sku.strip().lower()
            if inv_item.sku
            else inv_item.description.strip().lower()
        )

        wb_match = wb_items.get(k)
        po_match = po_items.get(k)

        if wb_match is None:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.UNFULFILLED_BILLING,
                    field=f"line_items[{idx}]",
                    expected=0.0,
                    actual=inv_item.quantity,
                    severity="error",
                    message=(
                        f"Invoice bills for '{inv_item.description}' (qty {inv_item.quantity}), "
                        f"but item was not received on Waybill {waybill.waybill_number}"
                    ),
                )
            )
        else:
            max_allowed_qty = wb_match.quantity * (1.0 + qty_tolerance_pct / 100.0)
            if inv_item.quantity > max_allowed_qty and not _isclose(
                inv_item.quantity, wb_match.quantity
            ):
                diff = round(inv_item.quantity - wb_match.quantity, 2)
                discrepancies.append(
                    Discrepancy(
                        type=DiscrepancyType.UNFULFILLED_BILLING,
                        field=f"line_items[{idx}].quantity",
                        expected=wb_match.quantity,
                        actual=inv_item.quantity,
                        difference=diff,
                        severity="error",
                        message=(
                            f"Unfulfilled billing on '{inv_item.description}': billed quantity {inv_item.quantity} "
                            f"exceeds Waybill delivered quantity {wb_match.quantity} by {diff}"
                        ),
                    )
                )

        if po_match is None:
            discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.UNORDERED_ITEM,
                    field=f"line_items[{idx}]",
                    expected=None,
                    actual=inv_item.description,
                    severity="error",
                    message=f"Unordered item on invoice: '{inv_item.description}' was not authorized on PO {po.po_number}",
                )
            )
        else:
            max_allowed_price = po_match.unit_price * (
                1.0 + price_tolerance_pct / 100.0
            )
            if inv_item.unit_price > max_allowed_price and not _isclose(
                inv_item.unit_price, po_match.unit_price
            ):
                diff = round(inv_item.unit_price - po_match.unit_price, 2)
                discrepancies.append(
                    Discrepancy(
                        type=DiscrepancyType.PRICE_VARIANCE,
                        field=f"line_items[{idx}].unit_price",
                        expected=po_match.unit_price,
                        actual=inv_item.unit_price,
                        difference=diff,
                        severity="error",
                        message=(
                            f"Price variance on '{inv_item.description}': billed unit price {inv_item.unit_price:.2f} "
                            f"exceeds PO authorized price {po_match.unit_price:.2f}"
                        ),
                    )
                )

    has_errors = any(d.severity == "error" for d in discrepancies)
    status = MatchingStatus.DISCREPANCY if has_errors else MatchingStatus.MATCHED

    return MatchResult(
        status=status,
        matched_amount=min(invoice.total_amount, po.total_amount),
        variance_amount=round(abs(invoice.total_amount - po.total_amount), 2),
        discrepancies=discrepancies,
        metadata={
            "po_number": po.po_number,
            "waybill_number": waybill.waybill_number,
            "invoice_number": invoice.invoice_number,
        },
    )
