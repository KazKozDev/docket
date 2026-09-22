"""Optional Docling/TableFormer layout backend."""
from __future__ import annotations

import importlib.util

from ..layout import CellHint, DocumentLayout, RawWord, TableHint, build_page
from .base import BackendStatus, Capabilities, OcrBackend, OcrError
from .source import DocumentSource, PageSource

INSTALL_HINT = 'Install it with `pip install "docket-idp[docling]"`'


def _get(value, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _bbox(box, page_height: float) -> tuple[float, float, float, float]:
    if box is None:
        raise ValueError("missing bounding box")
    left = float(_get(box, "l", _get(box, "x0", 0.0)))
    right = float(_get(box, "r", _get(box, "x1", 0.0)))
    top = float(_get(box, "t", _get(box, "y0", 0.0)))
    bottom = float(_get(box, "b", _get(box, "y1", 0.0)))
    origin = str(_get(box, "coord_origin", "")).lower()
    if "bottom" in origin or top > bottom:
        top, bottom = page_height - top, page_height - bottom
    x0, x1 = sorted((left, right))
    y0, y1 = sorted((top, bottom))
    return x0, y0, x1, y1


def _split_words(text: str, box: tuple[float, float, float, float], key: object) -> list[RawWord]:
    x0, y0, x1, y1 = box
    lines = text.splitlines() or [text]
    line_height = (y1 - y0) / max(1, len(lines))
    words: list[RawWord] = []
    for line_no, line in enumerate(lines):
        tokens = line.split()
        total = sum(len(token) for token in tokens) + max(0, len(tokens) - 1)
        cursor = x0
        step = (x1 - x0) / max(1, total)
        for token in tokens:
            end = cursor + len(token) * step
            words.append(
                RawWord(
                    token, cursor, y0 + line_no * line_height, end,
                    y0 + (line_no + 1) * line_height, line_key=(key, line_no),
                )
            )
            cursor = end + step
    return words


def document_from_docling(document, *, backend: str = "docling") -> DocumentLayout:
    """Translate a DoclingDocument (or a compatible recorded result) to docket."""
    pages = _get(document, "pages", {}) or {}
    page_data: dict[int, dict] = {}
    for number, page in (pages.items() if isinstance(pages, dict) else enumerate(pages, 1)):
        size = _get(page, "size", page)
        page_data[int(number)] = {
            "width": float(_get(size, "width", 1.0)),
            "height": float(_get(size, "height", 1.0)),
            "words": [],
            "hints": [],
            "table_boxes": [],
        }

    for table_no, table in enumerate(_get(document, "tables", []) or []):
        provenance = list(_get(table, "prov", []) or [])
        if not provenance:
            continue
        page_no = int(_get(provenance[0], "page_no", 1))
        if page_no not in page_data:
            continue
        page = page_data[page_no]
        cells = []
        raw_cells = _get(_get(table, "data", {}), "table_cells", []) or []
        for cell_no, cell in enumerate(raw_cells):
            raw_box = _get(cell, "bbox")
            if raw_box is None:
                continue
            x0, y0, x1, y1 = _bbox(raw_box, page["height"])
            row = int(_get(cell, "start_row_offset_idx", _get(cell, "row", 0)))
            column = int(_get(cell, "start_col_offset_idx", _get(cell, "column", 0)))
            row_end = int(_get(cell, "end_row_offset_idx", row + _get(cell, "row_span", 1)))
            col_end = int(_get(cell, "end_col_offset_idx", column + _get(cell, "column_span", 1)))
            text = str(_get(cell, "text", "") or "").strip()
            cells.append(
                CellHint(
                    row=row, column=column, row_span=max(1, row_end - row),
                    column_span=max(1, col_end - column), x0=x0, y0=y0, x1=x1, y1=y1,
                    text=text,
                )
            )
            page["words"].extend(_split_words(text, (x0, y0, x1, y1), ("table", table_no, cell_no)))
        if not cells:
            continue
        try:
            tx0, ty0, tx1, ty1 = _bbox(_get(provenance[0], "bbox"), page["height"])
        except ValueError:
            tx0, ty0 = min(c.x0 for c in cells), min(c.y0 for c in cells)
            tx1, ty1 = max(c.x1 for c in cells), max(c.y1 for c in cells)
        page["table_boxes"].append((tx0, ty0, tx1, ty1))
        page["hints"].append(TableHint(tx0, ty0, tx1, ty1, cells, detection="backend"))

    for text_no, item in enumerate(_get(document, "texts", []) or []):
        text = str(_get(item, "text", "") or "").strip()
        for prov_no, provenance in enumerate(_get(item, "prov", []) or []):
            page_no = int(_get(provenance, "page_no", 1))
            if not text or page_no not in page_data:
                continue
            page = page_data[page_no]
            try:
                box = _bbox(_get(provenance, "bbox"), page["height"])
            except ValueError:
                continue
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in page["table_boxes"]):
                continue
            page["words"].extend(_split_words(text, box, ("text", text_no, prov_no)))

    layouts = []
    for page_no in sorted(page_data):
        page = page_data[page_no]
        layouts.append(
            build_page(
                page_number=page_no, width=page["width"], height=page["height"], unit="pt",
                backend=backend, words=page["words"], table_hints=page["hints"],
            )
        )
    return DocumentLayout(pages=layouts)


class DoclingBackend(OcrBackend):
    name = "docling"
    capabilities = Capabilities(
        confidence=False, word_coordinates=True, lines=True, tables=True, rotation=True,
        languages=None, inputs=["image", "pdf"],
    )

    def __init__(self, settings=None) -> None:
        super().__init__(settings)
        self._document_cache: dict[tuple[str, int, int], DocumentLayout] = {}

    def availability(self) -> BackendStatus:
        if importlib.util.find_spec("docling") is None:
            return BackendStatus(
                name=self.name, available=False, reason="the 'docling' package is not installed",
                install_hint=INSTALL_HINT,
            )
        return BackendStatus(name=self.name, available=True)

    def _converter(self):
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

        options = PdfPipelineOptions(
            do_ocr=True, do_table_structure=True, generate_page_images=False,
        )
        options.table_structure_options.mode = (
            TableFormerMode.ACCURATE
            if self.settings.docling_table_mode == "accurate"
            else TableFormerMode.FAST
        )
        options.table_structure_options.do_cell_matching = self.settings.docling_cell_matching
        if hasattr(options.ocr_options, "lang"):
            options.ocr_options.lang = self.settings.languages
        return DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.IMAGE],
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
            },
        )

    def recognize_document(self, document: DocumentSource) -> DocumentLayout:
        self.require_available()
        if document.kind not in {"pdf", "image"}:
            raise OcrError(f"Docling cannot read {document.kind!r} sources")
        stat = document.path.stat()
        cache_key = (str(document.path.resolve()), stat.st_mtime_ns, stat.st_size)
        if cache_key in self._document_cache:
            return self._document_cache[cache_key]
        try:
            result = self._converter().convert(document.path)
            layout = document_from_docling(result.document, backend=self.name)
            self._document_cache = {cache_key: layout}
            return layout
        except Exception as exc:  # noqa: BLE001
            raise OcrError(f"Docling failed on {document.path.name}: {exc}") from exc

    def recognize_page(self, page: PageSource):
        layout = self.recognize_document(page.document)
        found = layout.page(page.number)
        if found is None:
            raise OcrError(f"Docling returned no page {page.number}")
        return found


__all__ = ["DoclingBackend", "document_from_docling"]
