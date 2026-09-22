from __future__ import annotations

from types import SimpleNamespace as NS

from docket.ocr import OcrSettings, get_ocr_backend
from docket.ocr.docling import DoclingBackend, document_from_docling
from docket.ocr.registry import backend_names


def _prov(page, box):
    return NS(page_no=page, bbox=NS(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin="TOPLEFT"))


def test_docling_document_maps_text_tables_wrapping_and_spans():
    cells = [
        NS(text="Description", bbox=_prov(1, (20, 100, 300, 130)).bbox,
           start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=1),
        NS(text="Amount", bbox=_prov(1, (300, 100, 500, 130)).bbox,
           start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=1, end_col_offset_idx=2),
        NS(text="Implementation\nsupport", bbox=_prov(1, (20, 130, 300, 190)).bbox,
           start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=0, end_col_offset_idx=1),
        NS(text="1200.00", bbox=_prov(1, (300, 130, 500, 190)).bbox,
           start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=1, end_col_offset_idx=2),
        NS(text="Grand total 1200.00", bbox=_prov(1, (20, 190, 500, 230)).bbox,
           start_row_offset_idx=2, end_row_offset_idx=3, start_col_offset_idx=0, end_col_offset_idx=2),
    ]
    table = NS(prov=[_prov(1, (20, 100, 500, 230))], data=NS(table_cells=cells))
    document = NS(
        pages={1: NS(size=NS(width=600, height=800))},
        tables=[table],
        texts=[NS(text="Invoice 42", prov=[_prov(1, (20, 30, 160, 50))])],
    )
    page = document_from_docling(document).pages[0]
    assert page.lines[0].text == "Invoice 42"
    assert page.tables[0].grid() == [
        ["Description", "Amount"],
        ["Implementation\nsupport", "1200.00"],
        ["Grand total 1200.00", ""],
    ]
    assert page.tables[0].cells[-1].column_span == 2
    assert page.text.count("Implementation") == 1


def test_docling_is_a_lazy_builtin_and_settings_are_typed():
    assert "docling" in backend_names()
    backend = get_ocr_backend(
        "docling", OcrSettings(docling_table_mode="fast", docling_cell_matching=False)
    )
    assert isinstance(backend, DoclingBackend)
    assert backend.settings.docling_table_mode == "fast"


def test_missing_docling_reports_optional_install(monkeypatch):
    monkeypatch.setattr("docket.ocr.docling.importlib.util.find_spec", lambda name: None)
    status = DoclingBackend().availability()
    assert not status.available
    assert "docket-idp[docling]" in status.install_hint
