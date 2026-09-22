"""The independent-OCR witness: when the vision model wins, its transcript
is cross-checked against what Tesseract read of the same page.

The witness may only *contradict*, never fail to confirm. An earlier version
required every extracted number to appear among the confident OCR readings —
absence of evidence read as evidence of error — and on a clean invoice it
accused nine correct fields at once, because a confidence-filtered reading is
a sparse, arbitrary subset: zip codes and date fragments made the list while
the actual amounts did not.

So the anchor is the cited line's own wording, with the numbers stripped.
Match that wording in the witness transcript and compare only there. No
keywords, no vendor lists, no auto-correction, and a witness that never read
that region stays silent.
"""
from datetime import date

from docket.layout import build_page, text_only_page
from docket.ocr import AcquisitionOptions, acquire
from docket.ocr.tesseract import page_confidence, words_from_data
from docket.ocr.witness import confident_amounts
from docket.catalog import Invoice, LineItem
from docket.validate import validate
from tests.factories import ScriptedBackend, flat_invoice, flat_po, words_page, write_png


def _invoice(**overrides):
    base = {
        "invoice_number": "001",
        "issue_date": date(2021, 7, 13),
        "due_date": date(2021, 8, 13),
        "vendor_name": "Saldo Apps",
        "customer_name": "Shepard corp.",
        "line_items": [
            LineItem(
                description="Prototype", quantity=2, unit_price=4000.0, total=8000.0
            )
        ],
        "subtotal": 8000.0,
        "tax_amount": 450.0,
        "total_amount": 8450.0,
        "field_locations": {
            "invoice_number": {"page": 1, "quote": "Invoice no. 001"},
            "issue_date": {"page": 1, "quote": "Jul 13 2021"},
            "vendor_name": {"page": 1, "quote": "Saldo Apps"},
            "customer_name": {"page": 1, "quote": "Shepard corp."},
            "subtotal": {"page": 1, "quote": "Subtotal: USD 8000.00"},
            "tax_amount": {"page": 1, "quote": "Sales Tax: USD 450.00"},
            "total_amount": {"page": 1, "quote": "Total: USD 8,450.00"},
        },
    }
    base.update(overrides)
    return flat_invoice(**base)


PRIMARY = (
    "[PAGE 1]\nInvoice no. 001\nJul 13 2021\nSaldo Apps\nShepard corp.\n"
    "Prototype | 2 | 4000.00 | 8000.00\n"
    "Subtotal: USD 8000.00\nSales Tax: USD 450.00\nTotal: USD 8,450.00"
)

# What Tesseract independently read of the same page.
WITNESS_AGREES = [
    "Prototype | 2 | 4000.00 | 8000.00\n"
    "Subtotal: USD 8000.00\nSales Tax: USD 450.00\nTotal: USD 8,450.00"
]


def _data(texts, confs, lefts, widths, lines=None):
    n = len(texts)
    return {
        "text": texts,
        "conf": [str(c) for c in confs],
        "left": [str(v) for v in lefts],
        "top": ["10"] * n if lines is None else [str(10 + 30 * (ln - 1)) for ln in lines],
        "width": [str(v) for v in widths],
        "height": ["14"] * n,
        "page_num": [1] * n,
        "block_num": [1] * n,
        "par_num": [1] * n,
        "line_num": [1] * n if lines is None else lines,
    }


def _page(data):
    return build_page(
        page_number=1,
        width=1000.0,
        height=1000.0,
        unit="px",
        backend="tesseract",
        confidence=page_confidence(data, 0.6),
        words=words_from_data(data),
    )


def _ocr_data(words):
    n = len(words)
    return _data(
        [w for w, _ in words], [c for _, c in words], [10 + 60 * i for i in range(n)], [50] * n
    )


def _chain(tmp_path, ocr_page, vlm_text):
    """Tesseract reading below the gate, then the vision model."""
    tesseract = ScriptedBackend("tesseract", {0: ocr_page})
    vlm = ScriptedBackend(
        "vlm",
        {0: text_only_page(page_number=1, text=vlm_text, backend="vlm")},
        geometry=False,
    )
    options = AcquisitionOptions(backend=tesseract, fallbacks=[vlm])
    return acquire(write_png(tmp_path / "page.png"), options)


def test_vlm_success_keeps_witness_numbers(tmp_path):
    ocr_page = words_page(["TOTAL 12.00"], confidence=0.3, word_confidence=0.9)
    result = _chain(tmp_path, ocr_page, "clean transcription")
    assert result.report.pages[0].backend == "vlm"
    assert result.witness_pages == ["TOTAL 12.00"]
    assert result.witness_numbers == [[12.0]]


def test_empty_ocr_leaves_no_witness(tmp_path):
    ocr_page = words_page([], confidence=0.0)
    result = _chain(tmp_path, ocr_page, "clean transcription")
    assert result.witness_pages == [None]
    assert result.witness_numbers == [[]]


def test_low_confidence_words_are_not_witnesses():
    data = _ocr_data([("8,480.00", 95), ("45O.OO", 12), ("8000.00", 30)])
    assert confident_amounts(_page(data), 0.6) == [8480.0]


def test_split_amount_is_glued_from_touching_boxes():
    data = _data(["8,480", ".00"], [90, 92], [10, 62], [50, 25])
    assert confident_amounts(_page(data), 0.6) == [8480.0]


def test_column_gap_becomes_separator():
    data = _data(["2", "15.00", "30.00"], [90, 91, 92], [10, 300, 600], [20, 50, 50])
    assert _page(data).text == "2 | 15.00 | 30.00"


def test_garbled_lines_sink_page_confidence():
    # two lines: one confident, one garbage — char-weighted share drops below gate
    data = _data(
        ["Subtotal", "145.00", "sata", '"4500', "zzz"],
        [95, 93, 20, 15, 10],
        [10, 200, 10, 200, 300],
        [80, 60, 50, 60, 40],
        lines=[1, 1, 2, 2, 2],
    )
    assert page_confidence(data, 0.6) < 0.6


def test_an_agreeing_witness_says_nothing():
    assert validate(_invoice(), PRIMARY, witness_pages=WITNESS_AGREES) == []


def test_a_contradicting_witness_is_flagged_not_corrected():
    """The case this exists for: a printed 450.00 that the vision model
    transcribed as 480.00, which is exactly the figure that makes the page
    add up. Tesseract read the same line and disagrees.
    """
    inv = _invoice(
        tax_amount=480.0,
        total_amount=8480.0,
        field_locations={
            "subtotal": {"page": 1, "quote": "Subtotal: USD 8000.00"},
            "tax_amount": {"page": 1, "quote": "Sales Tax: USD 480.00"},
            "total_amount": {"page": 1, "quote": "Total: USD 8,480.00"},
        },
    )
    issues = validate(inv, PRIMARY, witness_pages=WITNESS_AGREES)
    flagged = [
        i for i in issues if i.field == "tax_amount" and "independent OCR" in i.message
    ]
    assert flagged, [i.message for i in issues]
    assert inv.tax_amount == 480.0  # the validator reports; it never rewrites


def test_a_witness_that_never_read_the_line_stays_silent():
    """Absence is not evidence. This is the failure mode that flooded a clean
    invoice with nine accusations.
    """
    witness = ["New York, NY 12210\n3787 Pineview Drive\nCambridge, MA 12240"]
    assert validate(_invoice(), PRIMARY, witness_pages=witness) == []


def test_uncited_fields_are_not_cross_checked():
    # Line items carry no citation, so there is no line to anchor on and
    # nothing is claimed about them either way.
    inv = _invoice(
        line_items=[
            LineItem(
                description="Prototype",
                quantity=2000,
                unit_price=20230450.0,
                total=20230450.0,
            )
        ]
    )
    issues = validate(inv, PRIMARY, witness_pages=WITNESS_AGREES)
    assert not [
        i
        for i in issues
        if i.field.startswith("line_items") and "independent OCR" in i.message
    ]


def test_garbled_witness_skips_quietly():
    assert validate(_invoice(), PRIMARY, witness_pages=[None]) == []
    assert validate(_invoice(), PRIMARY, witness_pages=None) == []


def test_mixed_date_convention_is_flagged():
    inv = _invoice(issue_date=date(2019, 11, 2), due_date=date(2019, 2, 24))
    text = "[PAGE 1]\nInvoice date 11/02/2019\nDue 26/02/2019"
    issues = validate(inv, text)
    flagged = {i.field for i in issues if "convention" in i.message}
    assert {"issue_date", "due_date"} <= flagged


def test_consistent_dates_pass_convention_check():
    inv = _invoice(issue_date=date(2019, 2, 11), due_date=date(2019, 2, 26))
    text = "[PAGE 1]\nInvoice date 11/02/2019\nDue 26/02/2019"
    issues = validate(inv, text)
    assert not [i for i in issues if "convention" in i.message]


def test_ambiguous_only_document_skips_convention_check():
    inv = _invoice(issue_date=date(2019, 11, 2), due_date=None)
    issues = validate(inv, "[PAGE 1]\nInvoice date 11/02/2019")
    assert not [i for i in issues if "convention" in i.message]


def test_vlm_without_any_ocr_support_needs_review():
    """One document-level flag, not one accusation per field — with no
    independent reading at all, nothing specific can be said about which
    number is wrong.
    """
    issues = validate(_invoice(), PRIMARY, vlm_unconfirmed=True)
    assert (
        len([i for i in issues if i.field == "*" and "no confident OCR" in i.message])
        == 1
    )
    assert validate(_invoice(), PRIMARY, witness_pages=WITNESS_AGREES) == []


def test_a_lost_decimal_point_is_not_a_contradiction():
    """Tesseract drops decimal separators routinely: a golden-set receipt
    printing 7.75 came back as 775.00, and 5.00/15.00 as 500/1500. That is
    the witness being unreliable about the point, not the transcript being
    altered, so it must not accuse a correct value.
    """
    witness = ["Total: USD 775.00"]
    inv = _invoice(
        subtotal=7.75,
        tax_amount=0.0,
        total_amount=7.75,
        line_items=[],
        field_locations={"total_amount": {"page": 1, "quote": "Total: USD 7.75"}},
    )
    text = "[PAGE 1]\nTotal: USD 7.75"
    assert not [
        i
        for i in validate(inv, text, witness_pages=witness)
        if "independent OCR" in i.message
    ]


def test_a_changed_digit_survives_the_decimal_tolerance():
    # 450 against 480 is a different digit, not a moved point.
    witness = ["Sales Tax: USD 450.00"]
    inv = _invoice(
        tax_amount=480.0,
        total_amount=8480.0,
        field_locations={"tax_amount": {"page": 1, "quote": "Sales Tax: USD 480.00"}},
    )
    issues = validate(inv, PRIMARY, witness_pages=witness)
    assert [
        i for i in issues if i.field == "tax_amount" and "independent OCR" in i.message
    ]


def test_the_unconfirmed_advisory_is_a_warning_not_a_review_trigger():
    """It names no specific fault, so on its own it must not queue a correct
    extraction for a human.
    """
    issues = validate(_invoice(), PRIMARY, vlm_unconfirmed=True)
    advisory = [i for i in issues if i.field == "*" and "no confident OCR" in i.message]
    assert advisory and advisory[0].severity == "warning"


def test_verified_arithmetic_downgrades_witness_disagreement_to_warning():
    """When total_amount is proven by subtotal + taxes + line items,
    a noisy OCR witness reading does not trigger an error that queues
    the clean document for review.
    """
    witness = ["Total: USD 9450.00"]  # OCR misread 8 as 9
    inv = _invoice()
    issues = validate(inv, PRIMARY, witness_pages=witness)
    witness_issues = [
        i
        for i in issues
        if i.field == "total_amount" and "independent OCR" in i.message
    ]
    assert witness_issues
    assert all(i.severity == "warning" for i in witness_issues)


def test_decimal_comma_amounts_make_ambiguous_dates_day_first():
    """Found by the OCR benchmark: a French credit note dated 03/09/2026
    (3 September) came back as 9 March — OCR was perfect, the only date was
    ambiguous, and nothing told the model the document is European."""
    from docket.validate import _document_date_convention

    french = "[PAGE 1]\nAvoir n° AV-2026-031\nDate : 03/09/2026\nTotal TTC : 484,80 €"
    assert _document_date_convention(french) == "dmy"
    assert _document_date_convention("Invoice date 03/09/2026\nTotal: $1,800.00") == "mdy"  # dollar sign: US
    assert _document_date_convention("Date 03/09/2026\nTotal 1.278,00 and 12.50") is None  # mixed: undecided
    assert _document_date_convention("Invoice date 12/31/2026\nTotal 404,00") == "mdy"  # dates win
    # The dollar sign outranks the decimal comma: US-style invoices that print
    # comma amounts (the donut corpus: "$ 889,20") write month-first dates, and
    # their ambiguous dates came back day-first — five silent wrong answers.
    assert _document_date_convention("Date 06/02/2015\nTotal $ 889,20") == "mdy"
    assert _document_date_convention("Date 06/02/2015\nTotal AU$ 889.20") is None  # AU$ is day-first: abstain

    misread = _invoice(issue_date=date(2026, 3, 9), due_date=None)
    flagged = {i.field for i in validate(misread, french) if "convention" in i.message}
    assert "issue_date" in flagged
    right = _invoice(issue_date=date(2026, 9, 3), due_date=None)
    assert not [i for i in validate(right, french) if "convention" in i.message]


def test_item_witness_disagreement_is_a_warning_when_rows_close_their_sum():
    """A garbled witness contradicts an item value: if the rows still sum to
    the stated subtotal, that is verified arithmetic downgrading the
    contradiction to a warning — a correct extraction is not blocked on a
    bad independent reading."""
    inv = _invoice(
        subtotal=555.00,
        line_items=[LineItem(description="Sabanas algodon", quantity=30, unit_price=18.50, total=555.00)],
        field_locations={
            "line_items[0].quantity": {"page": 1, "quote": "Sabanas algodon 30 18.50 555.00"},
        },
    )
    witness = ["Sabanas algodon 150.00 18.50"]  # same row, misread numbers
    issues = validate(inv, PRIMARY, witness_pages=witness)
    item_issues = [i for i in issues if i.field == "line_items[0].quantity" and "independent OCR" in i.message]
    assert item_issues and all(i.severity == "warning" for i in item_issues)
