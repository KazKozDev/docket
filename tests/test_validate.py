from datetime import date

from docket.catalog import AcceptanceAct, AcceptanceActItem, BankStatement, BankStatementTransaction, Contract, Invoice, LineItem, PurchaseOrder, Receipt, ReceiptItem, Waybill, WaybillItem
from docket.validate import assess_contract_risks, validate
from tests.factories import flat_invoice, flat_po


def test_invoice_totals_match_no_issues():
    inv = flat_invoice(
        invoice_number="INV-001",
        issue_date=date(2026, 1, 1),
        due_date=date(2026, 1, 31),
        vendor_name="Acme Corp",
        vendor_tax_id="US-123456",
        customer_name="Wile E. Coyote",
        line_items=[
            LineItem(description="Widget", quantity=2, unit_price=50.0, total=100.0)
        ],
        subtotal=100.0,
        tax_amount=10.0,
        total_amount=110.0,
    )
    assert validate(inv) == []


def test_invoice_total_mismatch_is_flagged():
    inv = flat_invoice(
        invoice_number="INV-002",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        subtotal=100.0,
        tax_amount=10.0,
        total_amount=999.0,  # wrong on purpose
    )
    issues = validate(inv)
    assert any(i.field == "total_amount" for i in issues)


def test_invoice_due_before_issue_is_flagged():
    inv = flat_invoice(
        invoice_number="INV-003",
        issue_date=date(2026, 2, 1),
        due_date=date(2026, 1, 1),  # before issue_date
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        subtotal=50.0,
        total_amount=50.0,
    )
    issues = validate(inv)
    assert any(i.field == "due_date" for i in issues)


def test_receipt_items_sum_mismatch_is_flagged():
    rec = Receipt(
        merchant_name="Corner Store",
        transaction_date=date(2026, 1, 1),
        items=[
            ReceiptItem(description="Coffee", price=3.5),
            ReceiptItem(description="Bagel", price=2.5),
        ],
        total_amount=100.0,  # wrong on purpose
    )
    issues = validate(rec)
    assert any(i.field == "items" for i in issues)


def test_taxed_receipt_is_not_falsely_flagged():
    """Line items are pre-tax. Comparing them straight to the total flags
    every receipt that charges sales tax — reproduced on a real Walmart
    receipt whose own arithmetic (93.62 + 4.59 = 98.21) was perfect.
    """
    rec = Receipt(
        merchant_name="Walmart",
        transaction_date=date(2017, 7, 28),
        items=[
            ReceiptItem(description="PET TOY", price=90.70),
            ReceiptItem(description="DOG TREAT", price=2.92),
        ],
        subtotal=93.62,
        tax_amount=4.59,
        total_amount=98.21,
    )
    assert validate(rec) == []


def test_taxed_receipt_without_a_printed_subtotal_backs_the_tax_out():
    rec = Receipt(
        merchant_name="Corner Store",
        transaction_date=date(2026, 1, 1),
        items=[ReceiptItem(description="Coffee", price=10.00)],
        tax_amount=0.75,
        total_amount=10.75,
    )
    assert validate(rec) == []


def test_receipt_subtotal_plus_tax_must_equal_total():
    rec = Receipt(
        merchant_name="Corner Store",
        transaction_date=date(2026, 1, 1),
        subtotal=93.62,
        tax_amount=4.59,
        total_amount=120.00,  # wrong on purpose
    )
    issues = validate(rec)
    assert any(i.field == "total_amount" for i in issues)


def test_untaxed_receipt_still_validates():
    rec = Receipt(
        merchant_name="Corner Bean Coffee",
        transaction_date=date(2026, 9, 10),
        items=[
            ReceiptItem(description="Flat white", price=4.50),
            ReceiptItem(description="Croissant", price=3.25),
        ],
        total_amount=7.75,
    )
    assert validate(rec) == []


def test_receipt_with_tip_and_discount_validates():
    rec = Receipt(
        merchant_name="Le Bistro",
        transaction_date=date(2026, 3, 10),
        items=[
            ReceiptItem(
                description="Steak Frites", price=35.0, quantity=1.0, unit_price=35.0
            ),
            ReceiptItem(description="Wine", price=15.0, quantity=1.0, unit_price=15.0),
        ],
        subtotal=50.0,
        tax_amount=5.0,
        tip_amount=8.0,
        discount_amount=3.0,
        total_amount=60.0,
    )
    assert validate(rec) == []


def test_receipt_with_tip_and_discount_mismatch_flagged():
    rec = Receipt(
        merchant_name="Le Bistro",
        transaction_date=date(2026, 3, 10),
        subtotal=50.0,
        tax_amount=5.0,
        tip_amount=8.0,
        discount_amount=3.0,
        total_amount=75.0,  # Expected 60.0
    )
    issues = validate(rec)
    assert any(i.field == "total_amount" for i in issues)


def test_receipt_coupon_above_and_below_subtotal_both_validate():
    """Receipts print coupons in two places: above the SUBTOTAL line (the
    stated subtotal already has the discount applied) or below it (it doesn't).
    Both are consistent arithmetic and must pass; the validator used to flag
    the first layout no matter what the extraction said."""
    items = [
        ReceiptItem(description="PAINT ROLLER", price=8.99, quantity=1.0, unit_price=8.99),
        ReceiptItem(description="DROP CLOTH", price=12.5, quantity=1.0, unit_price=12.5),
    ]
    # Layout A: discount printed below the subtotal (items sum 21.49).
    below = Receipt(
        merchant_name="Northgate Hardware",
        transaction_date=date(2026, 4, 17),
        items=items,
        subtotal=21.49,
        tax_amount=2.21,
        discount_amount=2.0,
        total_amount=21.70,
    )
    # Layout B: coupon as a line above the SUBTOTAL, which already excludes it.
    above = Receipt(
        merchant_name="Northgate Hardware",
        transaction_date=date(2026, 4, 17),
        items=items,
        subtotal=19.49,
        tax_amount=2.21,
        discount_amount=2.0,
        total_amount=21.70,
    )
    assert validate(below) == []
    assert validate(above) == []


def test_receipt_coupon_mismatch_still_flagged():
    """Neither layout closes: the items rule and the total rule both fire."""
    rec = Receipt(
        merchant_name="Northgate Hardware",
        transaction_date=date(2026, 4, 17),
        items=[ReceiptItem(description="PAINT ROLLER", price=8.99, quantity=1.0, unit_price=8.99)],
        subtotal=9.99,  # items say 8.99, with discount 8.99 - 2.00 doesn't close either
        tax_amount=0.0,
        discount_amount=2.0,
        total_amount=10.50,  # closes under neither layout (7.99 nor 9.99)
    )
    issues = validate(rec)
    assert any(i.field == "items" for i in issues)
    assert any(i.field == "total_amount" for i in issues)


def test_receipt_item_quantity_unit_price_mismatch_flagged():
    rec = Receipt(
        merchant_name="Grocery Mart",
        transaction_date=date(2026, 2, 1),
        items=[
            ReceiptItem(
                description="Organic Apples",
                quantity=3.0,
                unit_price=4.0,
                price=10.0,  # 3 * 4 = 12 != 10
            )
        ],
        total_amount=10.0,
    )
    issues = validate(rec)
    assert any(i.field == "items[0]" for i in issues)


def test_receipt_merchant_tax_id_validation():
    # Valid Spanish CIF
    rec_valid = Receipt(
        merchant_name="Bar Tapas",
        merchant_tax_id="ESB12345674",
        transaction_date=date(2026, 5, 1),
        total_amount=25.0,
    )
    assert not any(i.field == "merchant_tax_id" for i in validate(rec_valid))

    # Invalid Spanish CIF checksum
    rec_invalid = Receipt(
        merchant_name="Bar Tapas",
        merchant_tax_id="ESB12345679",
        transaction_date=date(2026, 5, 1),
        total_amount=25.0,
    )
    issues = validate(rec_invalid)
    assert any(i.field == "merchant_tax_id" and i.severity == "error" for i in issues)


def test_invoice_valid_iban_has_no_issue():
    inv = flat_invoice(
        invoice_number="INV-004",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        vendor_iban="DE89370400440532013000",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert not any(i.field == "payment_account.iban" for i in issues)


def test_invoice_bad_iban_checksum_is_flagged():
    inv = flat_invoice(
        invoice_number="INV-005",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        vendor_iban="DE89370400440532013100",  # transposed digits
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(i.field == "payment_account.iban" and i.severity == "error" for i in issues)


def test_invoice_bad_vat_checksum_is_flagged():
    inv = flat_invoice(
        invoice_number="INV-006",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        vendor_vat_number="DE136695975",  # wrong check digit
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(i.field.startswith("seller.tax_ids") and i.severity == "error" for i in issues)


def test_invoice_unverifiable_vat_country_is_a_warning():
    inv = flat_invoice(
        invoice_number="INV-007",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        vendor_vat_number="JP12345678901",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(
        i.field.startswith("seller.tax_ids") and i.severity == "warning" for i in issues
    )


def test_invoice_valid_french_vat_has_no_issue():
    inv = flat_invoice(
        invoice_number="INV-008",
        issue_date=date(2026, 1, 1),
        vendor_name="Michelin",
        customer_name="Customer",
        vendor_vat_number="FR40303265045",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert not any(i.field.startswith("seller.tax_ids") for i in issues)


def test_invoice_bad_french_vat_checksum_is_error():
    inv = flat_invoice(
        invoice_number="INV-009",
        issue_date=date(2026, 1, 1),
        vendor_name="Michelin",
        customer_name="Customer",
        vendor_vat_number="FR40303265046",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(i.field.startswith("seller.tax_ids") and i.severity == "error" for i in issues)


def test_invoice_us_iban_informs_about_non_iban_system():
    inv = flat_invoice(
        invoice_number="INV-US-01",
        issue_date=date(2026, 1, 1),
        vendor_name="US Vendor",
        customer_name="Customer",
        vendor_iban="US12345678901234567890",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    iban_issue = next(i for i in issues if i.field == "payment_account.iban")
    assert "does not use IBAN" in iban_issue.message
    assert iban_issue.severity == "error"


def test_invoice_valid_us_ein_has_no_issue():
    inv = flat_invoice(
        invoice_number="INV-US-02",
        issue_date=date(2026, 1, 1),
        vendor_name="US Corp",
        customer_name="Customer",
        vendor_tax_id="12-3456789",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert not any(i.field.startswith("seller.tax_ids") for i in issues)


def test_invoice_invalid_us_ein_is_flagged():
    inv = flat_invoice(
        invoice_number="INV-US-03",
        issue_date=date(2026, 1, 1),
        vendor_name="US Corp",
        customer_name="Customer",
        vendor_tax_id="00-3456789",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(
        i.field.startswith("seller.tax_ids") and "US EIN" in i.message and i.severity == "error"
        for i in issues
    )


def test_invoice_valid_ca_bn_has_no_issue():
    inv = flat_invoice(
        invoice_number="INV-CA-01",
        issue_date=date(2026, 1, 1),
        vendor_name="Canadian Corp",
        customer_name="Customer",
        vendor_tax_id="123456782 RT 0001",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert not any(i.field.startswith("seller.tax_ids") for i in issues)


def test_invoice_invalid_ca_bn_is_flagged():
    inv = flat_invoice(
        invoice_number="INV-CA-02",
        issue_date=date(2026, 1, 1),
        vendor_name="Canadian Corp",
        customer_name="Customer",
        vendor_tax_id="123456783 RT 0001",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(
        i.field.startswith("seller.tax_ids")
        and "Canadian BN" in i.message
        and i.severity == "error"
        for i in issues
    )


def test_invoice_valid_brazil_cnpj_has_no_issue():
    inv = flat_invoice(
        invoice_number="INV-BR-01",
        issue_date=date(2026, 1, 1),
        vendor_name="Brazil Corp",
        customer_name="Customer",
        vendor_tax_id="11.222.333/0001-81",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert not any(i.field.startswith("seller.tax_ids") for i in issues)


def test_invoice_invalid_brazil_cnpj_is_flagged():
    inv = flat_invoice(
        invoice_number="INV-BR-02",
        issue_date=date(2026, 1, 1),
        vendor_name="Brazil Corp",
        customer_name="Customer",
        vendor_tax_id="11.222.333/0001-82",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(
        i.field.startswith("seller.tax_ids")
        and "Brazilian CNPJ" in i.message
        and i.severity == "error"
        for i in issues
    )


def test_invoice_customer_tax_id_validation():
    inv_valid = flat_invoice(
        invoice_number="INV-CUST-01",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Customer Corp",
        customer_tax_id="12-3456789",
        subtotal=100.0,
        total_amount=100.0,
    )
    assert not any(i.field.startswith("buyer.tax_ids") for i in validate(inv_valid))

    inv_invalid = flat_invoice(
        invoice_number="INV-CUST-02",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Customer Corp",
        customer_tax_id="11.222.333/0001-82",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv_invalid)
    assert any(i.field.startswith("buyer.tax_ids") and i.severity == "error" for i in issues)


def test_invoice_vendor_bic_validation():
    inv_valid = flat_invoice(
        invoice_number="INV-BIC-01",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Customer Corp",
        vendor_bic="DEUTDEDDFXX",
        subtotal=100.0,
        total_amount=100.0,
    )
    assert not any(i.field == "payment_account.bic" for i in validate(inv_valid))

    inv_invalid = flat_invoice(
        invoice_number="INV-BIC-02",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Customer Corp",
        vendor_bic="NOT_A_BIC_CODE_TOO_LONG",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv_invalid)
    assert any(i.field == "payment_account.bic" and i.severity == "warning" for i in issues)


def test_invoice_tax_rate_percent_validation():
    inv_match = flat_invoice(
        invoice_number="INV-TAX-01",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Customer Corp",
        subtotal=1000.0,
        tax_rate_percent=20.0,
        tax_amount=200.0,
        total_amount=1200.0,
    )
    assert not any(i.field == "tax_rate_percent" for i in validate(inv_match))

    inv_mismatch = flat_invoice(
        invoice_number="INV-TAX-02",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Customer Corp",
        subtotal=1000.0,
        tax_rate_percent=20.0,
        tax_amount=50.0,
        total_amount=1050.0,
    )
    issues = validate(inv_mismatch)
    assert any(
        i.field == "tax_rate_percent" and i.severity == "warning" for i in issues
    )


def test_contract_same_party_is_flagged():
    c = Contract(
        contract_title="Services Agreement",
        parties_a=["Acme Corp"],
        parties_b=["ACME CORP"],
        effective_date=date(2026, 1, 1),
    )
    issues = validate(c)
    assert any(i.field == "parties_b" for i in issues)


def test_invoice_with_discount_totals_correctly_has_no_issues():
    inv = flat_invoice(
        invoice_number="#001",
        issue_date=date(2025, 10, 1),
        due_date=date(2025, 10, 30),
        vendor_name="Your Company",
        customer_name="Client's Company Name",
        subtotal=5300.0,
        tax_amount=530.0,
        discount_amount=371.0,
        total_amount=5459.0,
    )
    assert validate(inv) == []


def test_contract_expiration_before_effective_is_flagged():
    c = Contract(
        contract_title="Services Agreement",
        parties_a=["Acme Corp"],
        parties_b=["Wile E. Coyote"],
        effective_date=date(2026, 6, 1),
        expiration_date=date(2026, 1, 1),
        key_obligations=["Deliver widgets monthly"],
    )
    issues = validate(c)
    assert any(i.field == "expiration_date" for i in issues)


def test_line_item_quantity_times_price_must_equal_line_total():
    """A garbled unit price is invisible to every other check: the line
    total, subtotal and grand total all still agree with each other.
    """
    inv = flat_invoice(
        invoice_number="INV-008",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        line_items=[
            LineItem(description="Item 2", quantity=1, unit_price=180.0, total=150.0)
        ],
        subtotal=150.0,
        total_amount=150.0,
    )
    issues = validate(inv)
    assert any(i.field == "line_items[0]" and i.severity == "error" for i in issues)


def test_consistent_line_items_pass():
    inv = flat_invoice(
        invoice_number="INV-009",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        line_items=[
            LineItem(description="Item 1", quantity=2, unit_price=100.0, total=200.0),
            LineItem(description="Item 3", quantity=2, unit_price=300.0, total=600.0),
        ],
        subtotal=800.0,
        total_amount=800.0,
    )
    assert validate(inv) == []


VLM_COLUMN_TEXT = (
    "SUB TOTAL\n$1250\nTAX (21%)\n$262\nDISCOUNT\n$0\nSHIPPING\n$0\nTOTAL AMOUNT\n$1512"
)


def _invoice(**overrides) -> Invoice:
    fields = dict(
        invoice_number="1254",
        issue_date=date(2023, 10, 5),
        vendor_name="Invoice Fly",
        customer_name="Sam Altman",
        subtotal=1250.0,
        tax_amount=262.0,
        total_amount=1512.0,
    )
    fields.update(overrides)
    return flat_invoice(**fields)


CONTRACT_TEXT = """SERVICES AGREEMENT

This Services Agreement is entered into as of 2026-03-01 by and between
Vertex Consulting LLC ("Party A") and Meridian Retail Inc. ("Party B").

Governing Law: State of Delaware.
This Agreement expires on 2027-03-01.
"""


def _contract(**overrides) -> Contract:
    fields = dict(
        contract_title="Services Agreement",
        parties_a=["Vertex Consulting LLC"],
        parties_b=["Meridian Retail Inc."],
        effective_date=date(2026, 3, 1),
        expiration_date=date(2027, 3, 1),
        governing_law="State of Delaware",
        key_obligations=["deliver monthly reports"],
    )
    fields.update(overrides)
    return Contract(**fields)


def test_contract_grounded_in_the_text_passes():
    assert validate(_contract(), CONTRACT_TEXT) == []


def test_invented_party_name_is_flagged():
    """A contract has no arithmetic to contradict a made-up party, so the
    only check left is whether the string is actually on the page.
    """
    issues = validate(_contract(parties_b=["Globex Corporation"]), CONTRACT_TEXT)
    assert any(
        i.field.startswith("parties_b") and i.severity == "error" for i in issues
    )


def test_party_name_punctuation_differences_are_tolerated():
    # "Meridian Retail Inc" vs the document's "Meridian Retail Inc." is a
    # rewrite, not a hallucination.
    assert validate(_contract(parties_b=["Meridian Retail Inc"]), CONTRACT_TEXT) == []


def test_date_written_in_another_format_is_still_found():
    text = CONTRACT_TEXT.replace("2026-03-01", "1 March 2026")
    issues = validate(_contract(), text)
    assert not any(i.field == "effective_date" for i in issues)


def test_date_absent_from_the_document_is_a_warning_not_an_error():
    """Contracts spell dates out in prose often enough that a miss is worth
    a human's glance, not an assertion that the extraction is wrong.
    """
    issues = validate(_contract(effective_date=date(2026, 7, 4)), CONTRACT_TEXT)
    assert any(i.field == "effective_date" and i.severity == "warning" for i in issues)


def test_invoice_date_far_in_the_future_is_flagged():
    # Reproduces a real hallucination: garbled OCR of the date line produced
    # "2036-01-01", which no other rule can contradict.
    inv = flat_invoice(
        invoice_number="1254",
        issue_date=date(2036, 1, 1),
        vendor_name="Invoice Fly",
        customer_name="Sam Altman",
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(inv)
    assert any(i.field == "issue_date" and "future" in i.message for i in issues)


def test_implausibly_old_date_is_flagged():
    inv = flat_invoice(
        invoice_number="X",
        issue_date=date(1887, 5, 1),
        vendor_name="Acme",
        customer_name="Bob",
        subtotal=10.0,
        total_amount=10.0,
    )
    issues = validate(inv)
    assert any(i.field == "issue_date" for i in issues)


def test_a_long_contract_term_is_not_flagged():
    # A 30-year lease is normal; the range check must not punish it.
    c = _contract(expiration_date=date(2056, 3, 1))
    issues = validate(c, CONTRACT_TEXT)
    assert not any(
        i.field == "expiration_date" and i.severity == "error" for i in issues
    )


def test_redacted_party_is_reported_as_redacted_not_as_missing():
    """A real CUAD contract redacts one party to "[ * * * ]". The model
    transcribed that faithfully — the field is unusable, but claiming the
    string isn't in the document would be false; it's there five times.
    """
    text = 'This Agreement is between [ * * * ] (the "Provider") and Vertex Consulting LLC.'
    c = _contract(
        parties_a=["[ * * * ]"],
        parties_b=["Vertex Consulting LLC"],
        governing_law=None,
        effective_date=date(2026, 3, 1),
        expiration_date=None,
    )
    issues = validate(c, text)
    party_issues = [i for i in issues if i.field.startswith("parties_a")]
    assert party_issues and "redact" in party_issues[0].message
    assert "does not appear" not in party_issues[0].message


def test_same_company_written_two_ways_is_flagged():
    """ "Acme Corp" and "Acme Corporation" are one counterparty. Comparing
    the raw strings says otherwise, which lets a contract with itself pass.
    """
    text = "This Agreement is between Acme Corp and Acme Corporation, effective 2026-03-01."
    c = _contract(
        parties_a=["Acme Corp"],
        parties_b=["Acme Corporation"],
        governing_law=None,
        expiration_date=None,
    )
    issues = validate(c, text)
    assert any("same entity" in i.message and i.severity == "error" for i in issues)


def test_parent_and_subsidiary_is_a_warning_not_an_error():
    # A parent contracting with its own subsidiary is unusual but legitimate,
    # so this is worth a look rather than a verdict.
    text = "Between Iberia S.A. and Iberia Mantenimiento S.A., effective 2026-03-01."
    c = _contract(
        parties_a=["Iberia S.A."],
        parties_b=["Iberia Mantenimiento S.A."],
        governing_law=None,
        expiration_date=None,
    )
    issues = validate(c, text)
    assert any(i.severity == "warning" and "share a name" in i.message for i in issues)


def test_genuinely_different_companies_pass():
    assert validate(_contract(), CONTRACT_TEXT) == []


def test_two_counterparties_on_one_side_are_kept_separate():
    """Reproduces a real CUAD contract: two entities are jointly one side.
    With a single string the model could only concatenate them, producing a
    name that appears nowhere in the document.
    """
    text = (
        "Services Agreement, by and between Vertex Consulting LLC (the 'Provider'), "
        "and TELCOSTAR PTE, LTD. and Ability Computer Ltd (each and both 'Recipient'), "
        "effective 2026-03-01."
    )
    c = _contract(
        parties_a=["Vertex Consulting LLC"],
        parties_b=["TELCOSTAR PTE, LTD.", "Ability Computer Ltd"],
        governing_law=None,
        expiration_date=None,
    )
    assert validate(c, text) == []


def test_an_empty_side_is_flagged():
    c = _contract(parties_b=[], governing_law=None, expiration_date=None)
    issues = validate(c, CONTRACT_TEXT)
    assert any(i.field == "parties_b" and "no party named" in i.message for i in issues)


def test_contract_value_without_currency_is_warning():
    c = _contract(contract_value=50000.0, currency=None)
    issues = validate(c, CONTRACT_TEXT)
    assert any(i.field == "currency" and i.severity == "warning" for i in issues)


def test_contract_currency_without_value_is_warning():
    c = _contract(currency="USD", contract_value=None)
    issues = validate(c, CONTRACT_TEXT)
    assert any(i.field == "contract_value" and i.severity == "warning" for i in issues)


def test_contract_excessive_notice_period_is_warning():
    c = _contract(notice_period_days=400)
    issues = validate(c, CONTRACT_TEXT)
    assert any(
        i.field == "notice_period_days" and i.severity == "warning" for i in issues
    )


def test_contract_payment_terms_absent_from_text_is_warning():
    c = _contract(payment_terms="Net 180 days upfront")
    issues = validate(c, CONTRACT_TEXT)
    assert any(i.field == "payment_terms" and i.severity == "warning" for i in issues)


def test_contract_valid_business_fields_passes():
    text = (
        CONTRACT_TEXT
        + "\nFees: 12000 USD. Terms: within 30 days. Notice: 30 days. Liability: capped at 12000 USD. Either party may terminate without cause."
    )
    c = _contract(
        contract_value=12000.0,
        currency="USD",
        payment_terms="within 30 days",
        auto_renewal=True,
        notice_period_days=30,
        liability_cap="capped at 12000 USD",
        termination_for_convenience=True,
    )
    assert validate(c, text) == []


def test_contract_excessive_cure_period_is_warning():
    c = _contract(cure_period_days=200)
    issues = validate(c, CONTRACT_TEXT)
    assert any(
        i.field == "cure_period_days" and i.severity == "warning" for i in issues
    )


def test_contract_liability_cap_absent_from_text_is_warning():
    c = _contract(liability_cap="10000000 USD maximum liability")
    issues = validate(c, CONTRACT_TEXT)
    assert any(i.field == "liability_cap" and i.severity == "warning" for i in issues)


def test_contract_phase2_fields_valid_passes():
    text = (
        CONTRACT_TEXT
        + "\nLiability: liability capped at 50000 USD. Termination for convenience upon 30 days notice. 30 days cure period. Non-solicitation of employees."
    )
    c = _contract(
        liability_cap="liability capped at 50000 USD",
        termination_for_convenience=True,
        cure_period_days=30,
        non_solicit=True,
    )
    assert validate(c, text) == []


def test_assess_contract_risks_detects_all_factors():
    c = _contract(
        contract_value=500000.0,
        currency="USD",
        liability_cap=None,
        auto_renewal=True,
        termination_for_convenience=False,
        notice_period_days=7,
        cure_period_days=90,
    )
    risks = assess_contract_risks(c)
    assert any("unlimited liability" in r for r in risks)
    assert any("auto-renewal trap" in r for r in risks)
    assert any("short notice period" in r for r in risks)
    assert any("long cure period" in r for r in risks)


def test_contract_signatories_absent_from_text_is_warning():
    c = _contract(signatories=["John Doe, President"])
    issues = validate(c, CONTRACT_TEXT)
    assert any("signatories[0]" in i.field and i.severity == "warning" for i in issues)


def test_contract_signatories_present_in_text_passes():
    text = CONTRACT_TEXT + "\nSigned by: John Doe, President."
    c = _contract(signatories=["John Doe, President"])
    issues = validate(c, text)
    assert not any("signatories" in i.field for i in issues)


# --- source citations -------------------------------------------------------
# These replace sixteen tests of a keyword search that had to be told, one
# document at a time, every place a money word can appear without being that
# field's amount: a tax ID, a column header, "Total excluding VAT", a "GST #"
# in footer boilerplate, the word "discount" inside a product name. The check
# below asks the model to point at its source and then verifies the pointer,
# which needs no vocabulary in any language.

TIMETREX_TEXT = """\
1 Bronze Support Package (5hrs, 10% discount) 449.95 1 449.95 H
Sub-Total: USD $5,749.90
HST: USD $689.99
Shipping: USD $16.86
Total: USD $6,456.75
invoice you are agreeing to the TimeTrex Terms of Use. GST #:
845942671
"""


def _sourced(**overrides) -> Invoice:
    fields = dict(
        invoice_number="16KLBPMO",
        issue_date=date(2012, 10, 29),
        vendor_name="TimeTrex",
        customer_name="John Doe",
        subtotal=5749.90,
        tax_amount=689.99,
        shipping_amount=16.86,
        total_amount=6456.75,
        field_locations={
            "subtotal": {"page": 1, "quote": "Sub-Total: USD $5,749.90"},
            "tax_amount": {"page": 1, "quote": "HST: USD $689.99"},
            "shipping_amount": {"page": 1, "quote": "Shipping: USD $16.86"},
            "total_amount": {"page": 1, "quote": "Total: USD $6,456.75"},
        },
    )
    fields.update(overrides)
    return flat_invoice(**fields)


def test_a_correctly_cited_invoice_is_clean():
    """The real TimeTrex invoice. The keyword search reported a tax of
    845942671 (the GST registration number in the footer) and a discount of
    1.00 (a row number inside a product description). Neither field is
    searched for any more — the citation is.
    """
    assert validate(_sourced(), TIMETREX_TEXT) == []


def test_a_tax_label_it_has_never_heard_of_still_works():
    # "HST" was in no keyword list, so the old search walked straight past
    # the real tax line. The model reads the label; we check the number.
    issues = validate(_sourced(tax_amount=42.00), TIMETREX_TEXT)
    assert any(i.field == "tax_amount" and "689.99" in i.message for i in issues)


def test_an_invented_citation_is_caught():
    inv = _sourced(
        field_locations={"tax_amount": {"page": 1, "quote": "Sales Tax: USD $999.00"}}
    )
    issues = validate(inv, TIMETREX_TEXT)
    assert any(
        i.field == "tax_amount" and "does not appear" in i.message for i in issues
    )


def test_a_value_that_contradicts_its_own_citation_is_caught():
    """The failure this exists for: the model cites the real line and then
    reports a different number, because it silently "corrected" the document.
    """
    inv = _sourced(
        tax_amount=480.00,
        field_locations={"tax_amount": {"page": 1, "quote": "Sales Tax: USD 450.00"}},
    )
    issues = validate(
        inv, "Subtotal: USD 8000.00\nSales Tax: USD 450.00\nTotal: USD 8,480.00"
    )
    assert any(i.field == "tax_amount" and "450.00" in i.message for i in issues)


def test_punctuation_and_spacing_differences_are_tolerated():
    inv = _sourced(
        field_locations={"tax_amount": {"page": 1, "quote": "HST:  USD  $689.99"}}
    )
    assert not any(i.field == "tax_amount" for i in validate(inv, TIMETREX_TEXT))


def test_uncited_fields_are_not_invented_problems():
    # The document states no discount, so the model cites none, so there is
    # nothing to verify — and nothing to falsely report.
    assert validate(_sourced(field_locations={}), TIMETREX_TEXT) == []


def test_european_amounts_in_citations():
    text = "Base imponible 1.234,56\nIVA 21% 259,26\nImporte total 1.493,82"
    inv = _sourced(
        subtotal=1234.56,
        tax_amount=259.26,
        shipping_amount=0.0,
        total_amount=1493.82,
        field_locations={
            "tax_amount": {"page": 1, "quote": "IVA 21% 259,26"},
            "total_amount": {"page": 1, "quote": "Importe total 1.493,82"},
        },
    )
    assert validate(inv, text) == []


def test_page_aware_input_requires_material_field_locations():
    issues = validate(_sourced(), "[PAGE 1]\n" + TIMETREX_TEXT)
    assert any(
        issue.field == "invoice_number" and "source-region" in issue.message
        for issue in issues
    )


def test_a_non_iban_is_reported_as_absent_not_as_a_bad_checksum():
    """A blank template still reading "[IBAN code]" is not an IBAN with a
    typo — saying so sends a reviewer hunting for a digit that isn't there.
    """
    inv = _sourced(vendor_iban="[IBAN code]")
    issues = validate(inv, TIMETREX_TEXT)
    iban = [i for i in issues if i.field == "payment_account.iban"]
    assert iban and "is not an IBAN" in iban[0].message
    assert "mod-97" not in iban[0].message


def test_a_real_iban_with_a_bad_digit_still_says_checksum():
    inv = _sourced(vendor_iban="DE89370400440532013100")
    issues = validate(inv, TIMETREX_TEXT)
    iban = [i for i in issues if i.field == "payment_account.iban"]
    assert iban and "mod-97" in iban[0].message


def test_a_derived_value_is_a_warning_not_an_error():
    """One real invoice printed "Total excl. VAT 372.00" and "Total incl.
    VAT 450.12" and no VAT line at all, so the tax could only be computed.
    Absence of a source is worth recording, not worth queueing a correct
    extraction for a human.
    """
    text = "[PAGE 1]\nTotal excl. VAT 372.00\nTotal incl. VAT 450.12"
    inv = _sourced(
        subtotal=372.00,
        tax_amount=78.12,
        shipping_amount=0.0,
        total_amount=450.12,
        field_locations={
            "subtotal": {"page": 1, "quote": "Total excl. VAT 372.00"},
            "total_amount": {"page": 1, "quote": "Total incl. VAT 450.12"},
        },
    )
    issues = validate(inv, text)
    derived = [i for i in issues if i.field == "tax_amount"]
    assert derived and derived[0].severity == "warning"
    assert "derived rather than read" in derived[0].message


def test_bank_statement_validates_cleanly():
    stmt = BankStatement(
        bank_name="Deutsche Bank",
        account_holder="Enterprise GmbH",
        account_iban="DE89370400440532013000",
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        currency="EUR",
        opening_balance=10000.0,
        closing_balance=15000.0,
        total_deposits=7000.0,
        total_withdrawals=2000.0,
        transactions=[
            BankStatementTransaction(
                transaction_date=date(2026, 1, 15),
                description="Client payment",
                amount=7000.0,
                balance_after=17000.0,
            ),
            BankStatementTransaction(
                transaction_date=date(2026, 1, 20),
                description="Office rent",
                amount=-2000.0,
                balance_after=15000.0,
            ),
        ],
    )
    assert validate(stmt) == []


def test_bank_statement_closing_balance_mismatch_flagged():
    stmt = BankStatement(
        bank_name="Deutsche Bank",
        account_holder="Enterprise GmbH",
        account_iban="DE89370400440532013000",
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        currency="EUR",
        opening_balance=10000.0,
        closing_balance=99999.0,  # wrong
        total_deposits=7000.0,
        total_withdrawals=2000.0,
    )
    issues = validate(stmt)
    assert any(i.field == "closing_balance" for i in issues)


def test_bank_statement_bad_iban_flagged():
    stmt = BankStatement(
        bank_name="Bank",
        account_holder="Person",
        account_iban="DE89370400440532013100",  # bad checksum
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        opening_balance=100.0,
        closing_balance=100.0,
    )
    issues = validate(stmt)
    assert any(i.field == "account_iban" for i in issues)


def test_acceptance_act_validates_cleanly():
    act = AcceptanceAct(
        act_number="ACT-001",
        act_date=date(2026, 3, 1),
        customer_name="Alpha Corp",
        contractor_name="Beta Services LLC",
        subtotal=1000.0,
        tax_amount=200.0,
        total_amount=1200.0,
        items=[
            AcceptanceActItem(
                description="Security Audit",
                quantity=1.0,
                unit_price=1000.0,
                total=1000.0,
            )
        ],
        claims_waived=True,
    )
    assert validate(act) == []


def test_acceptance_act_self_contracting_flagged():
    act = AcceptanceAct(
        act_number="ACT-001",
        act_date=date(2026, 3, 1),
        customer_name="Acme Corporation",
        contractor_name="Acme Corp",  # same entity
        subtotal=100.0,
        total_amount=100.0,
    )
    issues = validate(act)
    assert any(i.field == "contractor_name" for i in issues)


def test_acceptance_act_total_mismatch_flagged():
    act = AcceptanceAct(
        act_number="ACT-001",
        act_date=date(2026, 3, 1),
        customer_name="Client LLC",
        contractor_name="Vendor Inc",
        subtotal=1000.0,
        tax_amount=200.0,
        total_amount=1500.0,  # wrong
    )
    issues = validate(act)
    assert any(i.field == "total_amount" for i in issues)


def test_waybill_validates_cleanly():
    wb = Waybill(
        waybill_number="WB-101",
        waybill_date=date(2026, 4, 1),
        shipper_name="Supplier Logistics LLC",
        consignee_name="Retail Store Inc",
        items=[
            WaybillItem(
                item_name="Item A",
                quantity=20.0,
                unit_price=10.0,
                total_price=200.0,
                gross_weight_kg=100.0,
            ),
            WaybillItem(
                item_name="Item B",
                quantity=10.0,
                unit_price=20.0,
                total_price=200.0,
                gross_weight_kg=50.0,
            ),
        ],
        total_quantity=30.0,
        total_gross_weight_kg=150.0,
        total_amount=400.0,
    )
    assert validate(wb) == []


def test_waybill_quantity_mismatch_flagged():
    wb = Waybill(
        waybill_number="WB-101",
        waybill_date=date(2026, 4, 1),
        shipper_name="Supplier Logistics LLC",
        consignee_name="Retail Store Inc",
        items=[
            WaybillItem(item_name="Item A", quantity=20.0),
            WaybillItem(item_name="Item B", quantity=10.0),
        ],
        total_quantity=50.0,  # 20 + 10 != 50
    )
    issues = validate(wb)
    assert any(i.field == "total_quantity" for i in issues)


def test_purchase_order_validates_cleanly():
    po = flat_po(
        po_number="PO-999",
        po_date=date(2026, 2, 1),
        vendor_name="Vendor Inc",
        customer_name="Client LLC",
        subtotal=500.0,
        tax_amount=50.0,
        total_amount=550.0,
        line_items=[
            LineItem(
                sku="SKU-1",
                description="Item 1",
                quantity=5,
                unit_price=100.0,
                total=500.0,
            )
        ],
    )
    assert validate(po) == []


def test_a_non_vat_shaped_number_filed_as_vat_is_checked_as_a_tax_id():
    """The scheme comes from the model's reading of the label. "Vendor Tax
    ID: GB-771-4402" was filed as VAT for its GB prefix and then failed the
    VAT checksum it was never claiming to satisfy."""
    from docket.catalog import Party, TaxIdentifier

    inv = flat_invoice(
        invoice_number="INV-1", issue_date=date(2026, 6, 2), vendor_name="Northgate",
        customer_name="Iberia", subtotal=100.0, total_amount=100.0,
    )
    inv = inv.model_copy(update={"seller": Party(name="Northgate", tax_ids=[TaxIdentifier(value="GB-771-4402", scheme="vat")])})
    assert not [i for i in validate(inv) if i.severity == "error"]
    shaped = inv.model_copy(update={"seller": Party(name="Northgate", tax_ids=[TaxIdentifier(value="GB123456789", scheme="vat")])})
    assert any(i.field == "seller.tax_ids[0]" and i.severity == "error" for i in validate(shaped))
