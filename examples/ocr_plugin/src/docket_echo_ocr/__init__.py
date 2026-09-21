"""An OCR backend plugin: reads the text another system already produced.

Many scanners and DMSs write a `scan.png.txt` (or hOCR) next to each image.
This backend returns that text instead of running an engine — a template
for wrapping any OCR engine or service. A real engine with word boxes would
build `RawWord`s and call `docket.layout.build_page`, as the built-in
backends do, to get lines, columns and tables for free.
"""
from pathlib import Path

from docket.layout import text_only_page
from docket.ocr import BackendStatus, Capabilities, OcrBackend, OcrError


class SidecarTextBackend(OcrBackend):
    name = "sidecar"
    capabilities = Capabilities(
        confidence=False, word_coordinates=False, lines=False, tables=False, rotation=False
    )

    def availability(self) -> BackendStatus:
        return BackendStatus(name=self.name, available=True)

    def recognize_page(self, page):
        sidecar = Path(f"{page.document.path}.txt")
        if not sidecar.exists():
            raise OcrError(f"no sidecar text file {sidecar.name}")
        pages = sidecar.read_text(encoding="utf-8").split("\f")
        text = pages[page.number - 1] if page.number <= len(pages) else ""
        return text_only_page(page_number=page.number, text=text, backend=self.name)
