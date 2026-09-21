"""Typed page geometry shared by every OCR backend.

Coordinates are normalized to 0..1 with the origin at the top-left corner of
the page *as it reads upright* — after any rotation the backend applied. The
page's original `width`/`height` (in `unit`) are kept so a box converts back
to pixels or PDF points with `BoundingBox.to_absolute`.

Lines, blocks, columns and tables refer to words by id rather than embedding
copies, so a word's geometry has exactly one home in the serialized result.
"""
from __future__ import annotations

from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Unit = Literal["px", "pt"]


class BoundingBox(BaseModel):
    """Axis-aligned box in normalized page coordinates (0..1, top-left origin)."""

    model_config = ConfigDict(frozen=True)

    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "BoundingBox":
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError(f"inverted box: {self}")
        return self

    @classmethod
    def from_absolute(
        cls, x0: float, y0: float, x1: float, y1: float, width: float, height: float
    ) -> "BoundingBox":
        """Normalize a box given in the page's own units, clamping to the page."""
        if width <= 0 or height <= 0:
            raise ValueError("page width and height must be positive")

        def clamp(v: float) -> float:
            return min(1.0, max(0.0, v))

        nx0, nx1 = sorted((clamp(x0 / width), clamp(x1 / width)))
        ny0, ny1 = sorted((clamp(y0 / height), clamp(y1 / height)))
        return cls(x0=nx0, y0=ny0, x1=nx1, y1=ny1)

    @classmethod
    def union(cls, boxes: Iterable["BoundingBox"]) -> "BoundingBox":
        boxes = list(boxes)
        if not boxes:
            raise ValueError("union of no boxes")
        return cls(
            x0=min(b.x0 for b in boxes),
            y0=min(b.y0 for b in boxes),
            x1=max(b.x1 for b in boxes),
            y1=max(b.y1 for b in boxes),
        )

    def to_absolute(self, width: float, height: float) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1) in the page's own units — pixels or points."""
        return (self.x0 * width, self.y0 * height, self.x1 * width, self.y1 * height)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2

    def contains_center_of(self, other: "BoundingBox") -> bool:
        cx = (other.x0 + other.x1) / 2
        return self.x0 <= cx <= self.x1 and self.y0 <= other.center_y <= self.y1


class WordToken(BaseModel):
    id: str = Field(description="Stable id, 'p{page}-w{index}'.")
    text: str
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Backend's recognition confidence; None when it reports none (e.g. a PDF text layer).",
    )
    bbox: BoundingBox
    page: int = Field(ge=1)
    block_id: str | None = None
    line_id: str | None = None


class TextLine(BaseModel):
    id: str
    text: str = Field(description="Words joined left to right; ' | ' marks a column-sized gap.")
    bbox: BoundingBox
    word_ids: list[str]
    column: int | None = Field(
        default=None, description="Index into PageLayout.columns, when the page has text columns."
    )
    table_id: str | None = Field(
        default=None, description="Set when this line is a row of a detected table."
    )


class TextBlock(BaseModel):
    id: str
    bbox: BoundingBox
    line_ids: list[str]
    column: int | None = None


class Column(BaseModel):
    """A vertical run of text separated from its neighbours by a gutter."""

    index: int
    bbox: BoundingBox


class TableCell(BaseModel):
    text: str
    bbox: BoundingBox
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    word_ids: list[str] = Field(default_factory=list)


class Table(BaseModel):
    id: str
    bbox: BoundingBox
    rows: int = Field(ge=1)
    columns: int = Field(ge=1)
    cells: list[TableCell]
    detection: Literal["ruled", "aligned", "backend"] = Field(
        description=(
            "'ruled': drawn cell borders in a PDF; 'aligned': inferred from word "
            "alignment only; 'backend': reported by the OCR engine itself."
        )
    )

    def grid(self) -> list[list[str]]:
        """Cell text as rows × columns; a spanned cell's text sits in its top-left slot."""
        grid = [["" for _ in range(self.columns)] for _ in range(self.rows)]
        for cell in self.cells:
            if cell.row < self.rows and cell.column < self.columns:
                grid[cell.row][cell.column] = cell.text
        return grid

    def to_markdown(self) -> str:
        return "\n".join(
            "| " + " | ".join(c.replace("\n", " ").strip() for c in row) + " |"
            for row in self.grid()
        )


class PageLayout(BaseModel):
    page_number: int = Field(ge=1)
    width: float = Field(gt=0, description="Page width in `unit`, before normalization.")
    height: float = Field(gt=0)
    unit: Unit = "px"
    rotation: int = Field(
        default=0, description="Clockwise degrees the source was rotated to read upright."
    )
    backend: str = Field(description="OCR backend that produced this page.")
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Page-level confidence as the backend defines it; None if it reports none.",
    )
    words: list[WordToken] = Field(default_factory=list)
    lines: list[TextLine] = Field(default_factory=list, description="In reading order.")
    blocks: list[TextBlock] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    text: str = Field(
        default="", description="Serialized page text in reading order — what the LLM reads."
    )

    def word(self, word_id: str) -> WordToken | None:
        for w in self.words:
            if w.id == word_id:
                return w
        return None

    @property
    def has_geometry(self) -> bool:
        return bool(self.words)


class DocumentLayout(BaseModel):
    pages: list[PageLayout] = Field(default_factory=list)

    def page(self, number: int) -> PageLayout | None:
        for p in self.pages:
            if p.page_number == number:
                return p
        return None

    @property
    def page_texts(self) -> list[str]:
        return [p.text for p in self.pages]

    @property
    def text(self) -> str:
        """All pages, each introduced by a `[PAGE n]` marker the extractor cites."""
        return "\n".join(f"[PAGE {p.page_number}]\n{p.text}" for p in self.pages)

    @property
    def tables(self) -> list[Table]:
        return [t for p in self.pages for t in p.tables]


__all__ = [
    "BoundingBox",
    "Column",
    "DocumentLayout",
    "PageLayout",
    "Table",
    "TableCell",
    "TextBlock",
    "TextLine",
    "Unit",
    "WordToken",
]
