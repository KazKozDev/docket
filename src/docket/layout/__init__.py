"""Layout-first document model: words, lines, blocks, columns and tables with
normalized coordinates, plus the analysis that builds them and the
serialization the LLM reads."""
from .analysis import CellHint, RawWord, TableHint, build_page, text_only_page
from .locate import locate_quote
from .models import (
    BoundingBox,
    Column,
    DocumentLayout,
    PageLayout,
    Table,
    TableCell,
    TextBlock,
    TextLine,
    WordToken,
)
from .serialize import serialize_page

__all__ = [
    "BoundingBox",
    "CellHint",
    "Column",
    "DocumentLayout",
    "PageLayout",
    "RawWord",
    "Table",
    "TableCell",
    "TableHint",
    "TextBlock",
    "TextLine",
    "WordToken",
    "build_page",
    "locate_quote",
    "serialize_page",
    "text_only_page",
]
