"""An OCR fallback chain, and what happened on each page.

    python examples/ocr_fallback.py scan.pdf

The primary engine reads every page; a page whose confidence is below
DOCKET_OCR_MIN_CONFIDENCE goes to the next backend, and so on. Pages of a
born-digital PDF skip OCR entirely. The same chain from the shell:

    docket process scan.pdf --ocr-backend paddle --ocr-fallback tesseract --ocr-fallback vlm
"""
import sys

from docket import OcrOptions, ProcessOptions, ReviewOptions, process_document

options = ProcessOptions(
    ocr=OcrOptions(
        backend="auto",  # first installed of tesseract, paddle
        fallbacks=["vlm"],  # e.g. ["paddle", "vlm"] to try a second engine first
    ),
    review=ReviewOptions(enqueue=False),
)
result = process_document(sys.argv[1], options)

print("primary:", result.ocr.primary_backend, "| fallbacks used:", result.ocr.fallbacks_applied or "none")
for page in result.ocr.pages:
    tried = ", ".join(
        f"{a.backend}={a.outcome}" + (f" ({a.reason})" if a.reason else "") for a in page.attempts
    )
    flag = "  DEGRADED" if page.degraded else ""
    print(f"  page {page.page}: used {page.backend}{flag} — {tried}")
