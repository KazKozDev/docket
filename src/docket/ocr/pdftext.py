"""Born-digital PDF pages: words and ruled tables straight from the text layer.

Exact characters, exact boxes, no model — the cheapest and most reliable
backend whenever a page has a usable text layer. "Usable" is decided by
`text_layer_problem`: enough characters, and not dominated by `(cid:N)`
glyph references, which is what a PDF with unmapped fonts yields — text in
name only.

Rotation: pdfplumber reports boxes in displayed coordinates. When most
characters' baselines are not left-to-right on the displayed page, every
glyph box is turned upright before words are grouped, and
`PageLayout.rotation` records the clockwise turn applied.
"""
from __future__ import annotations

import re

from pdfplumber.utils import extract_words

from .. import config
from ..layout import PageLayout, RawWord, TableHint, build_page, cells_from_boxes
from .base import BackendStatus, Capabilities, OcrBackend, OcrError
from .source import PageSource

_CID = re.compile(r"\(cid:\d+\)")
# A text layer whose characters are mostly unmapped glyph ids is unusable.
MAX_CID_SHARE = 0.10


def text_layer_problem(page: PageLayout) -> str | None:
    """Why this text layer should not be trusted, or None if it's fine."""
    text = " ".join(w.text for w in page.words)
    chars = len(text.replace(" ", ""))
    if chars < config.MIN_CHARS_PER_PAGE:
        return f"text layer has {chars} characters (< {config.MIN_CHARS_PER_PAGE})"
    cid_chars = sum(len(m) for m in _CID.findall(text))
    if cid_chars / chars > MAX_CID_SHARE:
        return f"text layer is {cid_chars / chars:.0%} unmapped glyph ids (cid)"
    return None


def _fix_rotation(chars: list[dict]) -> int:
    """Clockwise turn that makes the displayed page read upright, voted by
    the baseline direction of each character's text matrix (display space,
    y up). pdfplumber's own `direction` field can't tell bottom-to-top text
    from top-to-bottom, so the matrix is read directly."""
    votes: dict[int, int] = {}
    for char in chars:
        matrix = char.get("matrix")
        if not matrix:
            continue
        a, b = round(matrix[0]), round(matrix[1])
        turn = {(1, 0): 0, (0, -1): 270, (-1, 0): 180, (0, 1): 90}.get((a, b))
        if turn is not None:
            votes[turn] = votes.get(turn, 0) + 1
    return max(votes, key=votes.__getitem__) if votes else 0


def _turn(x0: float, y0: float, x1: float, y1: float, turn: int, w: float, h: float):
    """Rotate a box on a w×h page clockwise by `turn` degrees."""
    if turn == 90:  # (x, y) -> (h - y, x)
        return h - y1, x0, h - y0, x1
    if turn == 270:  # (x, y) -> (y, w - x)
        return y0, w - x1, y1, w - x0
    if turn == 180:
        return w - x1, h - y1, w - x0, h - y0
    return x0, y0, x1, y1


def _table_hints(plumber_page, turn: int, w: float, h: float) -> list[TableHint]:
    hints = []
    try:
        tables = plumber_page.find_tables()
    except Exception:  # noqa: BLE001 — a page without parsable rulings has no tables
        return []
    for table in tables:
        # Index rows and columns on the upright boxes, so a turned page's
        # table reads in its upright order.
        cells = [_turn(c[0], c[1], c[2], c[3], turn, w, h) for c in table.cells if c]
        if not cells:
            continue
        hint_cells = cells_from_boxes(cells, tolerance=0.5)
        # A table with one row or one column is a box around text, not a table.
        if len({c.row for c in hint_cells}) < 2 or len({c.column for c in hint_cells}) < 2:
            continue
        bx = _turn(table.bbox[0], table.bbox[1], table.bbox[2], table.bbox[3], turn, w, h)
        hints.append(TableHint(x0=bx[0], y0=bx[1], x1=bx[2], y1=bx[3], cells=hint_cells))
    return hints


class PDFTextBackend(OcrBackend):
    name = "pdf_text"
    capabilities = Capabilities(
        confidence=False,
        word_coordinates=True,
        lines=True,
        tables=True,
        rotation=True,
        languages=None,
        inputs=["pdf"],
    )

    def availability(self) -> BackendStatus:
        return BackendStatus(name=self.name, available=True)

    def recognize_page(self, page: PageSource) -> PageLayout:
        if page.kind != "pdf":
            raise OcrError(f"{self.name} reads PDF text layers only, not {page.kind} sources")
        plumber_page = page.plumber_page
        w, h = float(plumber_page.width), float(plumber_page.height)
        try:
            chars = plumber_page.chars
            turn = _fix_rotation(chars)
            if turn:
                # Turn every glyph box upright first, then group words there,
                # so rotated text comes out in reading order, not reversed.
                upright = []
                for c in chars:
                    x0, y0, x1, y1 = _turn(c["x0"], c["top"], c["x1"], c["bottom"], turn, w, h)
                    upright.append({**c, "x0": x0, "x1": x1, "top": y0, "bottom": y1,
                                    "doctop": y0, "upright": True})
                raw = extract_words(upright, keep_blank_chars=False)
            else:
                raw = plumber_page.extract_words(keep_blank_chars=False)
        except Exception as exc:
            raise OcrError(f"could not read the text layer of page {page.number}: {exc}") from exc
        words = [
            RawWord(text=item["text"], x0=item["x0"], y0=item["top"], x1=item["x1"], y1=item["bottom"])
            for item in raw
        ]
        width, height = (h, w) if turn in (90, 270) else (w, h)
        return build_page(
            page_number=page.number,
            width=width,
            height=height,
            unit="pt",
            rotation=turn,
            backend=self.name,
            words=words,
            table_hints=_table_hints(plumber_page, turn, w, h),
        )


__all__ = ["PDFTextBackend", "text_layer_problem"]
