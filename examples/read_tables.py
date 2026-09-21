"""Read the tables docket found, as structured cells with coordinates.

    python examples/read_tables.py invoice.pdf

Tables come from drawn cell borders in a PDF ("ruled"), from word alignment
on any page ("aligned"), or from the OCR engine itself ("backend", e.g.
PaddleOCR with DOCKET_PADDLE_TABLES=true). No LLM is involved.
"""
import sys

from docket.ocr import AcquisitionOptions, acquire

layout = acquire(sys.argv[1], AcquisitionOptions(fallbacks=[])).layout
for table in layout.tables:
    page = next(p for p in layout.pages if table in p.tables)
    x0, y0, x1, y1 = table.bbox.to_absolute(page.width, page.height)
    print(f"page {page.page_number}, {table.rows}x{table.columns} ({table.detection}), "
          f"at ({x0:.0f}, {y0:.0f})–({x1:.0f}, {y1:.0f}) {page.unit}")
    print(table.to_markdown())
    for cell in table.cells:
        if cell.row_span > 1 or cell.column_span > 1:
            print(f"  spanning cell r{cell.row} c{cell.column}: {cell.row_span}x{cell.column_span} {cell.text!r}")
