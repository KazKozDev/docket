"""Stage 1 provenance: line-item and nested-field citations end to end —
resolving to page regions, flagging fabrications, handling duplicate text
and fuzzy matches, and yielding drawable highlights from the result alone.

Real born-digital PDFs read by the real pipeline, with only the extraction
model stubbed — the same pattern as test_document_result.py.
"""
import pymupdf as fitz
import pytest

from docket import extract as extract_module
from docket import pipeline
from docket.options import OcrOptions, ProcessOptions, ReviewOptions
from docket.result import DocumentStatus

LINES = [
    "INVOICE",
    "Invoice no: INV-2026-0042",
    "Issue date: 2026-03-02",
    "Vendor: Northgate Supplies Ltd",
    "VAT ID: DE136695976",
    "Bill to: Iberia Mantenimiento SA",
    "Pos  Description          Qty  Unit      Total",
    "1    Kopierpapier A4       40   4.90      196.00",
    "2    Toner schwarz        6    58.50     351.00",
    "Subtotal: EUR 547.00",
    "VAT: EUR 116.27",
    "Total due: EUR 663.27",
]

CITES = {
    "invoice_number": "Invoice no: INV-2026-0042",
    "issue_date": "Issue date: 2026-03-02",
    "seller.name": "Vendor: Northgate Supplies Ltd",
    "seller.tax_ids[0].value": "VAT ID: DE136695976",
    "buyer.name": "Bill to: Iberia Mantenimiento SA",
    "subtotal": "Subtotal: EUR 547.00",
    "tax_amount": "VAT: EUR 116.27",
    "total_amount": "Total due: EUR 663.27",
    "line_items[0].description": "1    Kopierpapier A4       40   4.90      196.00",
    "line_items[0].quantity": "1    Kopierpapier A4       40   4.90      196.00",
    "line_items[0].unit_price": "1    Kopierpapier A4       40   4.90      196.00",
    "line_items[0].total": "1    Kopierpapier A4       40   4.90      196.00",
    "line_items[1].description": "2    Toner schwarz        6    58.50     351.00",
    "line_items[1].quantity": "2    Toner schwarz        6    58.50     351.00",
    "line_items[1].unit_price": "2    Toner schwarz        6    58.50     351.00",
    "line_items[1].total": "2    Toner schwarz        6    58.50     351.00",
}


def _options():
    return ProcessOptions(ocr=OcrOptions(fallbacks=[]), review=ReviewOptions(enqueue=False))


def _pdf(path, lines=LINES, pages=1):
    with fitz.open() as document:
        for page_number in range(pages):
            page = document.new_page(width=595, height=842)
            body = lines if pages == 1 else lines[page_number]
            for n, line in enumerate(body):
                page.insert_text((72, 90 + 20 * n), line, fontsize=11)
        document.save(path)
    return path


def _payload(**overrides):
    payload = {
        "invoice_number": "INV-2026-0042",
        "issue_date": "2026-03-02",
        "seller": {"name": "Northgate Supplies Ltd",
                   "tax_ids": [{"value": "DE136695976", "scheme": "vat", "country_code": "DE"}]},
        "buyer": {"name": "Iberia Mantenimiento SA"},
        "currency": "EUR",
        "line_items": [
            {"description": "Kopierpapier A4", "quantity": 40, "unit_price": 4.90, "total": 196.00},
            {"description": "Toner schwarz", "quantity": 6, "unit_price": 58.50, "total": 351.00},
        ],
        "subtotal": 547.00,
        "tax_amount": 116.27,
        "total_amount": 663.27,
        "field_locations": {f: {"page": 1, "quote": q} for f, q in CITES.items()},
    }
    payload.update(overrides)
    return payload


def _process(tmp_path, monkeypatch, payload) -> "pipeline.DocumentResult":
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    return pipeline.process_document(_pdf(tmp_path / "inv.pdf"), _options())


@pytest.fixture
def result(tmp_path, monkeypatch):
    return _process(tmp_path, monkeypatch, _payload())


def test_every_line_item_field_resolves_to_its_row(result):
    sources = result.field_sources
    assert sources["line_items[1].quantity"].status == "verified"
    assert sources["line_items[1].quantity"].match == "exact"
    page = result.layout.pages[0]
    q = sources["line_items[1].quantity"]
    words = " ".join(page.word(i).text for i in q.word_ids)
    assert "Toner" in words and "58.50" in words
    # The quantity box sits on the second item's row, below the first's.
    assert sources["line_items[0].quantity"].bbox.y1 <= q.bbox.y0


def test_nested_party_tax_id_citation_resolves(result):
    source = result.field_sources["seller.tax_ids[0].value"]
    assert source.status == "verified"
    assert source.regions and source.regions[0].bbox is not None


def test_fabricated_line_item_value_is_flagged(tmp_path, monkeypatch):
    """The model claims item 2's total is 999.00 and cites the real row: the
    row does not contain the number, so the value is not grounded in the
    cited region — the error goes to review, not out the door."""
    bad_items = [
        {"description": "Kopierpapier A4", "quantity": 40, "unit_price": 4.90, "total": 196.00},
        {"description": "Toner schwarz", "quantity": 6, "unit_price": 58.50, "total": 999.00},
    ]
    result = _process(tmp_path, monkeypatch, _payload(line_items=bad_items))
    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert any("line_items[1].total" in i.field for i in result.validation_issues)
    assert any("999.00" in i.message for i in result.validation_issues)


def test_line_item_citation_to_absent_row_is_an_error(tmp_path, monkeypatch):
    """A citation whose quote is not on the page at all is unlocated, and the
    grounding check flags the value as fabricated."""
    cites = dict(CITES)
    cites["line_items[0].total"] = "Pos  Description          Qty  Unit      Total\n0  Blue widgets  2  5.00  10.00"
    result = _process(tmp_path, monkeypatch, _payload(field_locations={
        f: {"page": 1, "quote": q} for f, q in cites.items()}))
    assert result.field_sources["line_items[0].total"].status == "unlocated"
    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert any("line_items[0].total" in i.field for i in result.validation_issues)


def test_duplicate_row_text_is_conflicting_provenance(tmp_path, monkeypatch):
    """The same row prints twice (a repeated header block, a carbon copy):
    two exact regions, one ambiguous status — both boxes kept."""
    lines = LINES + ["2    Toner schwarz        6    58.50     351.00"]
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _payload())
    path = _pdf(tmp_path / "inv.pdf", lines)
    result = pipeline.process_document(path, _options())
    source = result.field_sources["line_items[1].total"]
    assert source.status == "conflicting"
    assert len(source.regions) == 2
    assert source.match == "exact"


def test_fuzzy_citation_gets_fuzzy_status(tmp_path, monkeypatch):
    """OCR garbled the quote the model remembered: one close window instead
    of an exact hit — provenance is still drawable, but says 'fuzzy'."""
    cites = dict(CITES)
    cites["line_items[1].total"] = "2    Toner schwars        6    58.50     351.00"
    result = _process(tmp_path, monkeypatch, _payload(field_locations={
        f: {"page": 1, "quote": q} for f, q in cites.items()}))
    source = result.field_sources["line_items[1].total"]
    assert source.status == "fuzzy" and source.match == "fuzzy"
    assert source.bbox is not None


def test_multipage_table_citation_reaches_page_two(tmp_path, monkeypatch):
    """Line items spread over pages: a page-2 citation must resolve against
    page 2's layout, not page 1's."""
    lines_page1 = LINES[:8]
    lines_page2 = [
        "3    Ordner breit          25   2.80      70.00",
        "Subtotal: EUR 617.00",
        "VAT: EUR 131.11",
        "Total due: EUR 748.11",
    ]
    payload = _payload(
        line_items=[
            {"description": "Kopierpapier A4", "quantity": 40, "unit_price": 4.90, "total": 196.00},
            {"description": "Toner schwarz", "quantity": 6, "unit_price": 58.50, "total": 351.00},
            {"description": "Ordner breit", "quantity": 25, "unit_price": 2.80, "total": 70.00},
        ],
        subtotal=617.00,
        tax_amount=131.11,
        total_amount=748.11,
        field_locations={
            **{f: {"page": 1, "quote": q} for f, q in CITES.items()
               if f not in ("subtotal", "tax_amount", "total_amount")},
            "line_items[2].description": {"page": 2, "quote": "3    Ordner breit          25   2.80      70.00"},
            "line_items[2].quantity": {"page": 2, "quote": "3    Ordner breit          25   2.80      70.00"},
            "line_items[2].unit_price": {"page": 2, "quote": "3    Ordner breit          25   2.80      70.00"},
            "line_items[2].total": {"page": 2, "quote": "3    Ordner breit          25   2.80      70.00"},
            "subtotal": {"page": 2, "quote": "Subtotal: EUR 617.00"},
            "tax_amount": {"page": 2, "quote": "VAT: EUR 131.11"},
            "total_amount": {"page": 2, "quote": "Total due: EUR 748.11"},
        },
    )
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    path = _pdf(tmp_path / "inv.pdf", [lines_page1, lines_page2], pages=2)
    result = pipeline.process_document(path, _options())
    source = result.field_sources["line_items[2].total"]
    assert source.page == 2 and source.status == "verified"
    assert source.regions[0].page == 2
    assert result.field_sources["total_amount"].page == 2


def test_highlights_draw_everything_from_the_result(result):
    """A viewer gets page, box and field for every citation — including line
    items — from the DocumentResult alone."""
    highlights = result.highlights()
    by_field = {field for field, _, _ in highlights}
    assert {"seller.name", "line_items[0].total", "line_items[1].quantity"} <= by_field
    for field, source, region in highlights:
        assert region.page == source.page and region.bbox.x1 > region.bbox.x0
    page1 = result.highlights(page=1)
    assert page1 and all(r.page == 1 for _, _, r in page1)
    # Drawable in page coordinates: the union box of the first item's row.
    page = result.layout.pages[0]
    _, _, region = next(h for h in highlights if h[0] == "line_items[0].quantity")
    x0, _y0, _x1, y1 = region.bbox.to_absolute(page.width, page.height)
    assert 70 <= x0 <= 78 and y1 <= page.height


def test_uncited_rows_need_review(tmp_path, monkeypatch):
    """Rows go into the e-invoice export; one no line confirms is not a success."""
    cites = {f: q for f, q in CITES.items() if not f.startswith("line_items")}
    result = _process(tmp_path, monkeypatch, _payload(field_locations={
        f: {"page": 1, "quote": q} for f, q in cites.items()}))
    assert result.status == DocumentStatus.NEEDS_REVIEW
    [issue] = [i for i in result.validation_issues if i.field == "line_items"]
    assert "row(s) cite no source line" in issue.message


def test_a_row_cited_by_any_of_its_values_counts_as_cited(tmp_path, monkeypatch):
    cites = {f: q for f, q in CITES.items() if not f.startswith("line_items") or f.endswith(".total")}
    result = _process(tmp_path, monkeypatch, _payload(field_locations={
        f: {"page": 1, "quote": q} for f, q in cites.items()}))
    assert not [i for i in result.validation_issues if i.field == "line_items"]

def test_derived_unit_price_is_grounded_by_its_row(tmp_path, monkeypatch):
    """A row that prints quantity 2 and total 4.98 grounds a unit price of
    2.49 even though it is never printed: the value is entailed by the row's
    own numbers. A fabricated value that no pair derives still fails."""
    row = "1    Kopierpapier A4        2    4.98"
    cites = {f: q for f, q in CITES.items() if not f.startswith("line_items")}
    cites.update({
        "line_items[0].quantity": row, "line_items[0].unit_price": row,
        "line_items[0].total": row, "line_items[0].description": row,
        "line_items[1].total": "2    Toner schwarz        6    58.50     351.00",
        "subtotal": "Subtotal: EUR 355.98", "total_amount": "Total due: EUR 472.25",
    })
    payload = _payload(
        line_items=[
            {"description": "Kopierpapier A4", "quantity": 2, "unit_price": 2.49, "total": 4.98},
            {"description": "Toner schwarz", "quantity": 6, "unit_price": 58.50, "total": 351.00},
        ],
        subtotal=355.98,
        tax_amount=116.27,
        total_amount=472.25,
        field_locations={f: {"page": 1, "quote": q} for f, q in cites.items()},
    )
    lines = LINES[:7] + [row, "2    Toner schwarz        6    58.50     351.00",
                         "Subtotal: EUR 355.98", "VAT: EUR 116.27", "Total due: EUR 472.25"]
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    result = pipeline.process_document(_pdf(tmp_path / "inv.pdf", lines), _options())
    assert result.status == DocumentStatus.SUCCEEDED, result.validation_issues
    assert not any("line_items[0].unit_price" in i.field for i in result.validation_issues)


def test_unprintable_quantity_one_is_implicit_single_item(tmp_path, monkeypatch):
    """Receipts print one price per row and no '1': a quantity of 1 is the
    implicit single item, grounded by the row, not a fabrication."""
    row = "1    Kopierpapier A4             4.98"
    cites = {f: q for f, q in CITES.items() if not f.startswith("line_items")}
    cites.update({
        "line_items[0].quantity": row, "line_items[0].unit_price": row,
        "line_items[0].total": row, "line_items[0].description": row,
        "subtotal": "Subtotal: EUR 4.98", "tax_amount": "VAT: EUR 1.03",
        "total_amount": "Total due: EUR 6.01",
    })
    payload = _payload(
        line_items=[{"description": "Kopierpapier A4", "quantity": 1, "unit_price": 4.98, "total": 4.98}],
        subtotal=4.98,
        tax_amount=1.03,
        total_amount=6.01,
        field_locations={f: {"page": 1, "quote": q} for f, q in cites.items()},
    )
    lines = LINES[:7] + [row, "Subtotal: EUR 4.98", "VAT: EUR 1.03", "Total due: EUR 6.01"]
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    result = pipeline.process_document(_pdf(tmp_path / "inv.pdf", lines), _options())
    assert result.status == DocumentStatus.SUCCEEDED, result.validation_issues
    assert not any("line_items[0].quantity" in i.field for i in result.validation_issues)
