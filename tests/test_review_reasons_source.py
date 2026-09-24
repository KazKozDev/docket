"""A key value read from words OCR was unsure of is not a success — unless
something independent of that reading confirms it."""

from datetime import date

from docket import BankTransaction, DocumentStatus, corroborate
from docket.layout import DocumentLayout
from docket.result import SourceLocation
from docket.review_reasons import key_field_confidence, reasons_for
from tests.factories import flat_invoice, flat_po, make_result, words_page

LINES = [
    "ACME GmbH",                 # seller.name
    "Invoice INV-7 date 2026-03-01",
    "Total amount due: 1815.00",
    "Net 1500.00",
]


def _result(confidence: dict[str, float], *, subtotal_line: int = 3, **invoice_fields):
    """An invoice result whose words carry the given confidence, by word text."""
    page = words_page(LINES)
    words = [w.model_copy(update={"confidence": confidence.get(w.text, 0.95)}) for w in page.words]
    page = page.model_copy(update={"words": words})

    def cite(line: int) -> SourceLocation:
        ids = [w.id for w in words if w.line_id == page.lines[line].id]
        return SourceLocation(page=1, quote=LINES[line], word_ids=ids, confidence=0.9)

    invoice = flat_invoice(**{
        "invoice_number": "INV-7", "issue_date": date(2026, 3, 1), "vendor_name": "ACME GmbH",
        "customer_name": "Buyer", "currency": "EUR", "subtotal": 1500.0, "tax_amount": 0.0,
        "total_amount": 1815.0, **invoice_fields,
    })
    return make_result(
        extracted=invoice.model_dump(mode="json", exclude={"field_locations"}),
        layout=DocumentLayout(pages=[page]),
        field_sources={
            "seller.name": cite(0), "invoice_number": cite(1), "issue_date": cite(1),
            "total_amount": cite(2), "subtotal": cite(subtotal_line),
        },
    )


def _low(result):
    return [r for r in reasons_for(result) if "low confidence" in r]


def test_a_doubtful_digit_in_the_total_needs_review():
    result = _result({"1815.00": 0.41})
    assert [r.split(" was")[0] for r in _low(result)] == ["total_amount"]


def test_only_the_value_words_count_not_the_label():
    # "Total amount due:" badly read, the amount itself clearly read.
    result = _result({"Total": 0.3, "amount": 0.3, "due:": 0.3})
    assert key_field_confidence(result)["total_amount"] == 0.95
    assert not _low(result)


def test_names_are_not_gated():
    result = _result({"ACME": 0.2, "GmbH": 0.2})
    assert "seller.name" not in key_field_confidence(result)
    assert not _low(result)


def test_amounts_that_reconcile_on_separate_lines_are_confirmed():
    # 1500 + 315 tax = 1815, printed on different lines: one misread digit
    # would have broken the sum, so doubt about the total is answered.
    result = _result({"1815.00": 0.41}, tax_amount=315.0)
    assert not _low(result)


def test_reconciliation_needs_separate_lines():
    result = _result({"1815.00": 0.41}, subtotal_line=2, subtotal=1815.0)
    assert _low(result)


def _review(result):
    reasons = reasons_for(result)
    return result.model_copy(update={"review_reasons": reasons, "needs_review": bool(reasons),
                                     "status": DocumentStatus.NEEDS_REVIEW if reasons else DocumentStatus.SUCCEEDED})


def test_a_matching_purchase_order_confirms_the_total():
    result = _review(_result({"1815.00": 0.41}))
    po = flat_po(po_number="PO-1", po_date=date(2026, 2, 1), vendor_name="ACME GmbH", customer_name="Buyer",
                 currency="EUR", subtotal=1500.0, total_amount=1815.0)

    confirmed = corroborate(result, purchase_order=po)

    assert confirmed.status == DocumentStatus.SUCCEEDED
    assert confirmed.corroborated["total_amount"] == "purchase order PO-1 total"


def test_a_payment_confirms_the_total_and_a_named_number():
    result = _review(_result({"1815.00": 0.41, "INV-7": 0.41}))
    payments = [
        BankTransaction(transaction_date=date(2026, 3, 20), amount=-99.0, currency="EUR", description="other"),
        BankTransaction(transaction_date=date(2026, 3, 21), amount=-1815.0, currency="EUR",
                        description="SEPA ACME GmbH INV 7", transaction_id="tx-42"),
    ]

    confirmed = corroborate(result, transactions=payments)

    assert confirmed.status == DocumentStatus.SUCCEEDED
    assert confirmed.corroborated == {"invoice_number": "bank transaction tx-42",
                                      "total_amount": "bank transaction tx-42"}


def test_corroboration_never_clears_a_failed_check_or_another_currency():
    result = _review(_result({"1815.00": 0.41}))
    failing = result.model_copy(update={"review_reasons": [*result.review_reasons, "validation error: x — y"]})
    usd = [BankTransaction(transaction_date=date(2026, 3, 21), amount=1815.0, currency="USD")]

    assert corroborate(result, transactions=usd).status == DocumentStatus.NEEDS_REVIEW
    after = corroborate(failing, transactions=[usd[0].model_copy(update={"currency": "EUR"})])
    assert after.status == DocumentStatus.NEEDS_REVIEW
    assert after.review_reasons == ["validation error: x — y"]


def test_documents_without_a_layout_fall_back_to_the_quote_confidence():
    result = _result({}).model_copy(update={"layout": None})
    assert key_field_confidence(result)["total_amount"] == 0.9


def test_two_values_on_one_line_answer_for_their_own_words():
    # "Invoice INV-7 date 2026-03-01": a doubtful INV-7 doesn't doubt the date.
    confidence = key_field_confidence(_result({"INV-7": 0.41}))
    assert confidence["invoice_number"] == 0.41
    assert confidence["issue_date"] == 0.95
