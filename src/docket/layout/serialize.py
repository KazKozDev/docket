"""Page layout → the text the LLM reads.

The structured layout stays the source of geometry; this is only its
reading-order rendering. It keeps one output line per layout line, writes a
column-sized gap inside a line as ` | `, and marks where a table or a text
column starts so the model knows rows belong together:

    [TABLE 1: 3 rows x 4 columns]
    Description | Qty | Price | Total
    Widget | 2 | 4.00 | 8.00
    [COLUMN 1]
    ...

Markers are on their own lines, so a verbatim quote of a data line never has
to include one.
"""
from __future__ import annotations

from .models import PageLayout


def serialize_page(page: PageLayout, *, markers: bool = True) -> str:
    if not page.lines:
        return page.text
    tables = {t.id: (n, t) for n, t in enumerate(page.tables, start=1)}
    out: list[str] = []
    current_table: str | None = None
    current_column: int | None = None
    for line in page.lines:
        if markers and line.table_id and line.table_id != current_table:
            n, table = tables[line.table_id]
            out.append(f"[TABLE {n}: {table.rows} rows x {table.columns} columns]")
        if markers and line.column is not None and line.column != current_column:
            out.append(f"[COLUMN {line.column + 1}]")
        current_table = line.table_id
        current_column = line.column
        out.append(line.text)
    return "\n".join(out)


__all__ = ["serialize_page"]
