"""Vendor templates: rules read the page deterministically, citations included;
a template that doesn't hold falls back to the model without a wrong answer.

Born-digital PDFs through the real pipeline, with the extraction model
stubbed — the same pattern as test_line_item_citations.py — plus synthetic
layouts for the rule mechanics.
"""
import pytest

import docket.templates as templates_module
from docket import catalog, extract as extract_module, pipeline
from docket.catalog import SchemaSpec
from docket.layout.models import BoundingBox, DocumentLayout, PageLayout, Table, TableCell, TextLine
from docket.options import OcrOptions, ProcessOptions, ReviewOptions
from docket.result import DocumentStatus
from docket.templates import (
    FieldRule,
    ItemsRule,
    VendorTemplate,
    extract_with_template,
    match_vendor_template,
    register_vendor_template,
    unregister_vendor_template,
)

LINES = [
    "INVOICE",
    "Northgate Supplies Ltd",
    "Invoice no: INV-2026-0042",
    "Issue date: 2026-03-02",
    "VAT ID: DE136695976",
    "Bill to:",
    "Iberia Mantenimiento SA",
    "Subtotal: EUR 547.00",
    "VAT: EUR 116.27",
    "Total due: EUR 663.27",
]

NORTHGATE = VendorTemplate(
    template_id="test-northgate",
    schema_id="invoice",
    issuer=("Northgate Supplies",),
    fields=(
        FieldRule(field="invoice_number", label="Invoice no", value=r"Invoice no:\s*(\S+)"),
        FieldRule(field="issue_date", label="Issue date", value=r"Issue date:\s*([\d-]+)"),
        FieldRule(field="seller.name", value=r"(Northgate Supplies Ltd)"),
        FieldRule(field="seller.tax_ids[0].value", label="VAT ID", value=r"VAT ID:\s*([A-Z0-9]+)"),
        FieldRule(field="buyer.name", label="Bill to", value=r"^(.+?)\s*$", lines_after=1),
        FieldRule(field="currency", label="Total due", value=r"\b(EUR|USD)\b"),
        FieldRule(field="subtotal", label="Subtotal", value=r"Subtotal:\s*EUR\s*([\d.]+)"),
        FieldRule(field="tax_amount", label="VAT", value=r"VAT:\s*EUR\s*([\d.]+)"),
        FieldRule(field="total_amount", label="Total due", value=r"Total due:\s*EUR\s*([\d.]+)"),
    ),
)


def _page(text: str, page_number: int = 1) -> PageLayout:
    lines = [
        TextLine(id=f"l{i}", text=line, bbox=BoundingBox(x0=0, y0=i / 100, x1=1, y1=i / 100 + 0.005), word_ids=[])
        for i, line in enumerate(text.splitlines())
    ]
    return PageLayout(page_number=page_number, width=595, height=842, backend="test", lines=lines)


def _layout(text: str) -> DocumentLayout:
    return DocumentLayout(pages=[_page(text)])


@pytest.fixture
def northgate():
    register_vendor_template(NORTHGATE)
    yield NORTHGATE
    unregister_vendor_template(NORTHGATE.template_id)


def _options():
    return ProcessOptions(ocr=OcrOptions(fallbacks=[]), review=ReviewOptions(enqueue=False))


def _pdf(path, lines=LINES):
    import pymupdf as fitz

    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        for n, line in enumerate(lines):
            page.insert_text((72, 90 + 20 * n), line, fontsize=11)
        document.save(path)
    return path


# ---- the rules ---------------------------------------------------------------------------------


def test_lines_after_reads_the_line_below_the_label():
    layout = _layout("Bill to:\nIberia Mantenimiento SA")
    rule = FieldRule(field="buyer.name", label="Bill to", value=r"^(.+?)\s*$", lines_after=1)
    page, quote, captured = templates_module._find_rule(layout, rule)
    assert captured == "Iberia Mantenimiento SA"
    assert quote.startswith("Bill to:")  # the citation names the label line


def test_a_label_line_is_not_its_own_value_when_lines_after_is_set():
    """Declaring lines_after says the value is below; otherwise `^(.+)$`
    would capture the label line itself."""
    layout = _layout("Bill to:\nIberia Mantenimiento SA")
    rule = FieldRule(field="buyer.name", label="Bill to", value=r"^(.+?)\s*$")
    assert templates_module._find_rule(layout, rule) is None or (
        templates_module._find_rule(layout, rule)[2] == "Bill to:"
    )
    with_lines_after = FieldRule(field=rule.field, label=rule.label, value=rule.value, lines_after=1)
    assert templates_module._find_rule(layout, with_lines_after)[2] == "Iberia Mantenimiento SA"


def test_diacritic_fold_matches_ocr_that_lost_the_umlaut(northgate):
    """Tesseract reads 'Bürobedarf' as 'Burobedarf'; issuer and label matching
    fold diacritics on both sides, value regexes stay raw."""
    assert match_vendor_template("Nordlicht Burobedarf GmbH\nRECHNUNG", "invoice") is not None
    assert match_vendor_template("Some other company", "invoice") is None
    # a template belongs to its schema
    assert match_vendor_template("Northgate Supplies somewhere", "receipt") is None


def test_match_requires_every_issuer_pattern(northgate):
    two = VendorTemplate(
        template_id="test-two-issuer",
        schema_id="invoice",
        issuer=("Northgate Supplies", "RECHNUNG"),
        fields=(FieldRule(field="invoice_number", value=r"(\S+)"),),
    )
    register_vendor_template(two)
    try:
        assert match_vendor_template("Northgate only", "invoice") is None
        assert match_vendor_template("Northgate Supplies and RECHNUNG", "invoice") is not None
    finally:
        unregister_vendor_template(two.template_id)


def test_register_rejects_an_issuer_that_matches_everything():
    """Issuer patterns name the vendor; an empty list would match every
    document of the schema."""
    with pytest.raises(ValueError, match="issuer"):
        register_vendor_template(
            VendorTemplate(template_id="test-greedy", schema_id="invoice", issuer=(), fields=())
        )


def test_register_rejects_a_duplicate_id(northgate):
    with pytest.raises(ValueError, match="already registered"):
        register_vendor_template(NORTHGATE)


def test_template_api_is_exported_from_docket():
    from docket import VendorTemplate as PublicVendorTemplate
    from docket import list_vendor_templates as public_list

    assert PublicVendorTemplate is VendorTemplate
    assert {template.template_id for template in public_list()} >= {
        "nordlicht-buerobedarf-invoice",
        "distribuciones-albufera-invoice",
    }


def test_builtin_templates_are_marked_builtin():
    builtin_ids = {template.template_id for template in templates_module.BUILTIN_TEMPLATES}
    registered = {template.template_id: template for template in templates_module.list_vendor_templates()}
    assert all(registered[template_id].builtin for template_id in builtin_ids)


@pytest.mark.parametrize(
    "filename,template_id,number,total",
    [
        ("invoice_de_scan.jpg", "nordlicht-buerobedarf-invoice", "RE-2026-1187", 734.23),
        ("invoice_es_lowres_scan.jpg", "distribuciones-albufera-invoice", "A-2026/0457", 1546.38),
    ],
)
def test_builtin_templates_read_their_golden_scans(filename, template_id, number, total):
    from pathlib import Path

    from docket.ocr import AcquisitionOptions, acquire

    path = Path(__file__).parents[1] / "eval" / "golden_dataset" / filename
    acquisition = acquire(path, AcquisitionOptions(backend="tesseract", fallbacks=[]))
    template = match_vendor_template(acquisition.text, "invoice")
    assert template is not None and template.template_id == template_id
    instance = extract_with_template(acquisition.layout, catalog.get_schema("invoice"), template)
    assert instance is not None
    assert instance.invoice_number == number
    assert instance.total_amount == total


def test_amounts_and_dates_are_coerced_to_the_schema_types(northgate):
    layout = _layout(
        "Northgate Supplies Ltd\n"
        "Invoice no: INV-2026-0042\n"
        "Issue date: 14.07.2026\n"
        "Bill to:\nIberia Mantenimiento SA\n"
        "Subtotal: EUR 617,00\n"
        "Total due: EUR 734,23\n"
        "IBAN: DE89 3704 0044 0532 0130 00\n"
    )
    spec = catalog.get_schema("invoice")
    template = VendorTemplate(
        template_id="test-coerce",
        schema_id="invoice",
        issuer=("Northgate Supplies",),
        fields=(
            FieldRule(field="invoice_number", label="Invoice no", value=r"Invoice no:\s*(\S+)"),
            FieldRule(field="issue_date", label="Issue date", value=r"Issue date:\s*([\d.]+)"),
            FieldRule(field="seller.name", value=r"(Northgate Supplies Ltd)"),
            FieldRule(field="buyer.name", label="Bill to", value=r"^(.+?)\s*$", lines_after=1),
            FieldRule(field="subtotal", label="Subtotal", value=r"Subtotal:\s*EUR\s*([\d.,]+)"),
            FieldRule(field="total_amount", label="Total due", value=r"Total due:\s*EUR\s*([\d.,]+)"),
            FieldRule(field="payment_account.iban", label="IBAN", value=r"IBAN:\s*([A-Z0-9 ]+)", strip=True),
        ),
    )
    instance = extract_with_template(layout, spec, template)
    assert instance is not None
    assert str(instance.issue_date) == "2026-07-14"  # dotted date, day-first by convention
    assert instance.total_amount == 734.23  # decimal comma
    assert instance.payment_account.iban == "DE89370400440532013000"  # groups stripped
    assert instance.field_locations["issue_date"].quote.startswith("Issue date")


def test_a_rule_that_misses_kills_the_whole_template_reading(northgate):
    """The pipeline reads a template only whole: a missing rule means the
    model runs, not a half-filled document."""
    spec = catalog.get_schema("invoice")
    template = VendorTemplate(
        template_id="test-incomplete",
        schema_id="invoice",
        issuer=("Northgate Supplies",),
        fields=(
            FieldRule(field="invoice_number", label="Invoice no", value=r"Invoice no:\s*(\S+)"),
            FieldRule(field="issue_date", label="Issue date", value=r"Issue date:\s*([\d-]+)"),
            FieldRule(field="seller.name", value=r"(Northgate Supplies Ltd)"),
            FieldRule(field="buyer.name", value=r"(Nobody)"),
            FieldRule(field="subtotal", label="Subtotal", value=r"Subtotal:\s*EUR\s*([\d.]+)"),
            FieldRule(field="total_amount", label="Total due", value=r"Total due:\s*EUR\s*([\d.]+)"),
        ),
    )
    assert extract_with_template(_layout("\n".join(LINES)), spec, template) is None


def test_items_are_read_from_the_table_grid_with_row_citations():
    spec = catalog.get_schema("invoice")
    cells = []
    for row, values in enumerate((("Pos", "Desc", "Qty", "Unit", "Total"),
                                   ("1", "Kopierpapier A4", "40", "4,90", "196,00"),
                                   ("2", "Toner", "6", "58,50", "351,00"))):
        for column, text in enumerate(values):
            cells.append(TableCell(text=text, bbox=BoundingBox(x0=column / 10, y0=row / 10, x1=column / 10 + 0.05, y1=row / 10 + 0.05), row=row, column=column))
    table = Table(id="p1-t0", bbox=BoundingBox(x0=0, y0=0, x1=1, y1=1), rows=3, columns=5, cells=cells, detection="ruled")
    page = _page(
        "Northgate Supplies Ltd\nInvoice no: INV-1\nIssue date: 2026-03-02\nVAT ID: DE136695976\n"
        "Bill to:\nSomeone SA\nSubtotal: EUR 547.00\nVAT: EUR 116.27\nTotal due: EUR 663.27"
    ).model_copy(update={"tables": [table]})
    layout = DocumentLayout(pages=[page])
    template = VendorTemplate(
        template_id="test-items",
        schema_id="invoice",
        issuer=("Northgate Supplies",),
        fields=NORTHGATE.fields,
        items=ItemsRule(columns={"description": 1, "quantity": 2, "unit_price": 3, "total": 4}),
    )
    instance = extract_with_template(layout, spec, template)
    assert instance is not None
    assert [(i.description, i.quantity, i.unit_price, i.total) for i in instance.line_items] == [
        ("Kopierpapier A4", 40.0, 4.9, 196.0),
        ("Toner", 6.0, 58.5, 351.0),
    ]
    # every row field cites its own row's text, decimal commas coerced
    assert instance.field_locations["line_items[0].unit_price"].quote == "1 Kopierpapier A4 40 4,90 196,00"


def test_pipeline_reads_a_matched_document_without_the_model(tmp_path, monkeypatch, northgate):
    """The whole point: a matched document costs zero LLM extraction calls."""
    monkeypatch.setattr(
        extract_module, "extract_pages",
        lambda *a, **k: pytest.fail("the extraction model ran for a templated document"),
    )
    result = pipeline.process_document(_pdf(tmp_path / "inv.pdf"), _options())
    assert result.status == DocumentStatus.SUCCEEDED
    assert result.metrics.template_id == "test-northgate"
    assert result.metrics.llm_calls == 0
    assert result.extracted["invoice_number"] == "INV-2026-0042"
    assert result.extracted["buyer"]["name"] == "Iberia Mantenimiento SA"
    assert result.extracted["total_amount"] == 663.27
    # citations resolve to page geometry like any model reading
    total = result.field_sources["total_amount"]
    assert total.status == "verified"
    assert total.bbox is not None
    assert not [i for i in result.validation_issues if i.severity == "error"]


def test_a_reading_that_does_not_validate_falls_back_to_the_model(tmp_path, monkeypatch):
    """A template whose numbers don't add up must never reach the user: the
    model runs and the template match is recorded nowhere."""
    called = {"extract": False}

    def fake_extract(pages, model_cls, **kwargs):
        called["extract"] = True

        class _Stub(model_cls):
            pass

        from datetime import date

        instance = model_cls(
            invoice_number="INV-2026-0042",
            issue_date=date(2026, 3, 2),
            seller={"name": "Northgate Supplies Ltd", "tax_ids": [{"value": "DE136695976"}]},
            buyer={"name": "Iberia Mantenimiento SA"},
            currency="EUR",
            subtotal=547.0,
            tax_amount=116.27,
            total_amount=663.27,
            field_locations={
                "invoice_number": {"page": 1, "quote": "Invoice no: INV-2026-0042"},
                "issue_date": {"page": 1, "quote": "Issue date: 2026-03-02"},
                "seller.name": {"page": 1, "quote": "Northgate Supplies Ltd"},
                "seller.tax_ids[0].value": {"page": 1, "quote": "VAT ID: DE136695976"},
                "buyer.name": {"page": 1, "quote": "Iberia Mantenimiento SA"},
                "subtotal": {"page": 1, "quote": "Subtotal: EUR 547.00"},
                "tax_amount": {"page": 1, "quote": "VAT: EUR 116.27"},
                "total_amount": {"page": 1, "quote": "Total due: EUR 663.27"},
            },
        )
        return instance, 1

    monkeypatch.setattr(pipeline, "extract_pages", fake_extract)

    wrong = VendorTemplate(
        template_id="test-wrong-total",
        schema_id="invoice",
        issuer=("Northgate Supplies",),
        fields=(
            FieldRule(field="invoice_number", label="Invoice no", value=r"Invoice no:\s*(\S+)"),
            FieldRule(field="issue_date", label="Issue date", value=r"Issue date:\s*([\d-]+)"),
            FieldRule(field="seller.name", value=r"(Northgate Supplies Ltd)"),
            FieldRule(field="seller.tax_ids[0].value", label="VAT ID", value=r"VAT ID:\s*([A-Z0-9]+)"),
            FieldRule(field="buyer.name", label="Bill to", value=r"^(.+?)\s*$", lines_after=1),
            FieldRule(field="currency", label="Total due", value=r"\b(EUR|USD)\b"),
            FieldRule(field="subtotal", label="Subtotal", value=r"Subtotal:\s*EUR\s*([\d.]+)"),
            FieldRule(field="tax_amount", label="VAT", value=r"VAT:\s*EUR\s*([\d.]+)"),
            FieldRule(field="total_amount", label="Subtotal", value=r"([\d.]+)"),  # reads 547.00 as the total
        ),
    )
    register_vendor_template(wrong)
    try:
        result = pipeline.process_document(_pdf(tmp_path / "inv.pdf"), _options())
    finally:
        unregister_vendor_template(wrong.template_id)
    assert called["extract"] is True
    assert result.metrics.template_id is None
    assert result.status == DocumentStatus.SUCCEEDED
    assert result.extracted["total_amount"] == 663.27


def test_unmatched_document_reads_through_the_model_as_before(tmp_path, monkeypatch):
    """No vendor match -> the model runs, exactly as before stage 4."""
    from datetime import date

    calls = {"n": 0}

    def fake_extract(pages, model_cls, **kwargs):
        calls["n"] += 1
        instance = model_cls(
            invoice_number="INV-2026-0042",
            issue_date=date(2026, 3, 2),
            seller={"name": "Whoever Ltd"},
            buyer={"name": "Iberia Mantenimiento SA"},
            subtotal=547.0,
            tax_amount=116.27,
            total_amount=663.27,
            field_locations={
                "invoice_number": {"page": 1, "quote": "Invoice no: INV-2026-0042"},
                "issue_date": {"page": 1, "quote": "Issue date: 2026-03-02"},
                "seller.name": {"page": 1, "quote": "Whoever Ltd"},
                "buyer.name": {"page": 1, "quote": "Iberia Mantenimiento SA"},
                "subtotal": {"page": 1, "quote": "Subtotal: EUR 547.00"},
                "tax_amount": {"page": 1, "quote": "VAT: EUR 116.27"},
                "total_amount": {"page": 1, "quote": "Total due: EUR 663.27"},
            },
        )
        return instance, 1

    monkeypatch.setattr(pipeline, "extract_pages", fake_extract)
    result = pipeline.process_document(
        _pdf(tmp_path / "inv.pdf", [l.replace("Northgate Supplies Ltd", "Whoever Ltd") for l in LINES]),
        _options(),
    )
    assert calls["n"] == 1
    assert result.metrics.template_id is None
    assert result.status == DocumentStatus.SUCCEEDED
