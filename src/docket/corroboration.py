"""Confirm a result's key fields from other documents: `corroborate()`.

OCR can be unsure of a total that is nevertheless right. The purchase order
it was billed against, or the bank payment that settled it, states the same
amount independently of how this page was read. When one of them agrees,
the doubt about that field is answered and it no longer needs a person.

Only doubt is cleared, never a failed check: an invoice whose arithmetic
doesn't add up is wrong whatever the purchase order says.
"""
from __future__ import annotations

from collections.abc import Sequence

from .catalog import BankStatement, PurchaseOrder
from .result import DocumentResult, DocumentStatus
from .review_reasons import is_low_confidence_reason
from .schemas import BankTransaction
from .validate import _alnum


def corroborate(
    result: DocumentResult,
    *,
    purchase_order: PurchaseOrder | None = None,
    transactions: Sequence[BankTransaction] | BankStatement | None = None,
    amount_tolerance: float = 0.01,
) -> DocumentResult:
    """Mark the fields other documents confirm, and drop the review reasons
    that only doubted those fields.

    - `purchase_order`: its total and subtotal confirm the document's own,
      in the same currency.
    - `transactions` (bank or card lines, or a whole `BankStatement`): a
      payment of the document's total in its currency confirms the total;
      if the payment's text also carries the document number, that too.

    Returns a new result; `corroborated` records what confirmed each field.
    A result already written to the review queue stays there — removing it
    is the caller's decision.
    """
    document = result.document
    if document is None:
        return result
    currency = (getattr(document, "currency", None) or "").upper()
    confirmed = dict(result.corroborated)

    def amount(name: str) -> float | None:
        value = getattr(document, name, None)
        return float(value) if isinstance(value, (int, float)) and value else None

    if purchase_order is not None and purchase_order.currency.upper() == currency:
        for name in ("total_amount", "subtotal"):
            ours, theirs = amount(name), getattr(purchase_order, name, None)
            if ours is not None and theirs is not None and abs(ours - theirs) <= amount_tolerance:
                confirmed.setdefault(name, f"purchase order {purchase_order.po_number} {name.replace('_amount', '')}")

    number_field = next((f for f in ("invoice_number", "credit_note_number", "receipt_number")
                         if isinstance(getattr(document, f, None), str) and _alnum(getattr(document, f))), None)
    total = amount("total_amount")
    for label, tx_currency, tx_amount, text in _payments(transactions):
        if total is None or tx_currency != currency or abs(abs(tx_amount) - abs(total)) > amount_tolerance:
            continue
        if number_field is not None and _alnum(getattr(document, number_field)) in _alnum(text):
            confirmed[number_field] = label
            confirmed["total_amount"] = label
            break
        confirmed.setdefault("total_amount", label)

    if confirmed == result.corroborated:
        return result
    reasons = [r for r in result.review_reasons if not any(is_low_confidence_reason(r, f) for f in confirmed)]
    status = result.status
    if result.error is None:
        status = DocumentStatus.NEEDS_REVIEW if reasons else DocumentStatus.SUCCEEDED
    return result.model_copy(
        update={"corroborated": confirmed, "review_reasons": reasons, "needs_review": bool(reasons), "status": status}
    )


def _payments(transactions) -> list[tuple[str, str, float, str]]:
    """(label, currency, amount, searchable text) for each payment line."""
    if transactions is None:
        return []
    if isinstance(transactions, BankStatement):
        return [
            (
                f"{transactions.bank_name} statement line {tx.transaction_date.isoformat()}",
                transactions.currency.upper(),
                tx.amount,
                " ".join(filter(None, (tx.description, tx.reference, tx.counterparty_name))),
            )
            for tx in transactions.transactions
        ]
    return [
        (
            f"bank transaction {tx.transaction_id or tx.transaction_date.isoformat()}",
            tx.currency.upper(),
            tx.amount,
            tx.description,
        )
        for tx in transactions
    ]


__all__ = ["corroborate"]
