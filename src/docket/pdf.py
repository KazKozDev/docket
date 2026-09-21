"""PDF page counting and rasterization via pypdfium2.

pypdfium2 (Apache-2.0 / BSD-3) is already pulled in by pdfplumber, so using it
here keeps the dependency tree free of AGPL code (PyMuPDF) and lets docket be
embedded in closed-source applications.
"""
from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image


def page_count(path: str | Path) -> int:
    pdf = pdfium.PdfDocument(str(path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def render_page(path: str | Path, page_number: int, dpi: int) -> Image.Image:
    """Render one zero-indexed page to an RGB image at the given resolution."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        return _render(pdf[page_number], dpi)
    finally:
        pdf.close()


def render_pages(path: str | Path, dpi: int) -> list[Image.Image]:
    pdf = pdfium.PdfDocument(str(path))
    try:
        return [_render(page, dpi) for page in pdf]
    finally:
        pdf.close()


def _render(page: pdfium.PdfPage, dpi: int) -> Image.Image:
    bitmap = page.render(scale=dpi / 72)  # type: ignore[arg-type]  # float scale is supported
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()
        page.close()
