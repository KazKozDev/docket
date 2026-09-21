"""Read a scan with PaddleOCR and look at what it found — no LLM needed.

    pip install "docket-idp[paddle]"
    python examples/ocr_paddle.py scan.png

The first run downloads PaddleOCR's models to ~/.paddlex/official_models;
later runs work offline. DOCKET_PADDLE_MODEL=medium picks the larger models,
DOCKET_PADDLE_TABLES=true adds PaddleOCR's table-structure pipeline.
"""
import sys

from docket.ocr import AcquisitionOptions, OcrSettings, acquire

settings = OcrSettings(languages=["en", "de"], paddle_tables=True)
result = acquire(sys.argv[1], AcquisitionOptions(backend="paddle", fallbacks=[], settings=settings))

for page, record in zip(result.layout.pages, result.report.pages):
    print(f"page {page.page_number}: {record.backend}, confidence {page.confidence:.2f}, "
          f"rotated {page.rotation}°, {len(page.words)} words, {len(page.tables)} tables")
    print(page.text)
