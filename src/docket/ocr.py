"""Text acquisition: born-digital PDF text first, OCR for scans, VLM as a
last resort when OCR confidence is too low to trust.

This is the "pragmatic" layer the job spec asks for — reach for the
cheapest tool that works before reaching for the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pytesseract
from PIL import Image

from . import amounts, config, pdf as pdf_render
from .llm_client import LLMError, vision_transcribe

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}


@dataclass
class OcrResult:
    text: str
    method: str  # "pdf_text" | "ocr" | "vlm" | "ocr_degraded" | "mixed"
    pages: list[str] | None = None
    page_methods: list[str] | None = None
    # Independent OCR-layer text per page, kept only where the primary text
    # came from the vision model. Lets validation cross-check a claimed
    # number against a second reading instead of trusting one transcript.
    # None where there is no second layer (primary already is OCR/text).
    witness_pages: list[str | None] | None = None
    # Confident OCR numbers per page, parallel to witness_pages. Only words
    # Tesseract itself reports at or above the confidence floor count — a
    # garbled reading must stay silent, never accuse a correct extraction.
    witness_numbers: list[list[float]] | None = None

    def __post_init__(self) -> None:
        if self.pages is None:
            self.pages = [self.text]
        if self.page_methods is None:
            self.page_methods = [self.method] * len(self.pages)
        if self.witness_pages is None:
            self.witness_pages = [None] * len(self.pages)
        if self.witness_numbers is None:
            self.witness_numbers = [[] for _ in self.pages]


def _vlm_or_degraded_ocr(
    image_paths: list[Path],
    ocr_text: str,
    witness_numbers: list[list[float]] | None = None,
) -> OcrResult:
    """Transcribe with the vision model, falling back to the OCR text we
    already have if the model is unreachable, out of memory, or too slow.

    Losing a whole document because the optional, expensive tier timed out
    is the wrong failure mode: low-confidence text that a human can review
    beats no text at all. The "ocr_degraded" method marks it so downstream
    (review_queue) knows this document didn't get the treatment it needed.
    """
    try:
        chunks = [vision_transcribe(str(p)) for p in image_paths]
        witness = ocr_text if ocr_text.strip() else None
        numbers = witness_numbers or [[] for _ in chunks]
        return OcrResult(
            text="\n".join(chunks),
            method="vlm",
            pages=chunks,
            page_methods=["vlm"] * len(chunks),
            witness_pages=[witness] * len(chunks),
            witness_numbers=numbers,
        )
    except LLMError:
        if not ocr_text.strip():
            raise  # nothing salvageable — the caller should see the failure
        return OcrResult(text=ocr_text, method="ocr_degraded")


def _render_pdf_page(path: Path, page_number: int) -> Image.Image:
    return pdf_render.render_page(path, page_number, config.OCR_DPI)


def _line_words(
    data: dict, index: list[int]
) -> tuple[list[str], list[int], list[int], list[int]]:
    """One image_to_data row-group, left-to-right: text, confidence, offset, width."""
    rows = []
    for i in index:
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        try:
            conf = int(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < 0:
            continue
        rows.append((int(data["left"][i]), word, conf, int(data["width"][i])))
    rows.sort(key=lambda r: r[0])
    words = [r[1] for r in rows]
    return words, [r[2] for r in rows], [r[0] for r in rows], [r[3] for r in rows]


def _serialize_line(words: list[str], lefts: list[int], widths: list[int]) -> str:
    """Join a line's words left-to-right, marking wide gaps as column breaks.

    Pure box geometry — no words are read. A gap several characters wide
    means the layout has columns (QTY | PRICE | AMOUNT); flattening it to
    single spaces is what let quantities slide into neighbouring columns.
    """
    if not words:
        return ""
    char_width = sorted(widths)[len(widths) // 2] if widths else 0
    parts = [words[0]]
    for k in range(1, len(words)):
        gap = lefts[k] - (lefts[k - 1] + widths[k - 1])
        parts.append(" | " if char_width and gap > 3 * char_width else " ")
        parts.append(words[k])
    return "".join(parts)


def _glued_amounts(
    words: list[str],
    confs: list[int],
    lefts: list[int],
    widths: list[int],
    floor: float,
) -> list[float]:
    """Parse numbers from box-adjacent word runs, not isolated tokens.

    Tesseract often splits one printed amount across words ("8,480" ".00").
    A per-word regex misses both halves; joining runs whose boxes touch
    reads what the page actually shows. Every word in the run must clear
    the floor, or the run contributes nothing.
    """

    def _parse(text: str) -> list[float]:
        found = []
        for match in amounts.MONEY_RE.finditer(text):
            value = amounts.parse_amount(match.group(1))
            if value is not None:
                found.append(value)
        return found

    amounts_out: list[float] = []
    run, run_confs = [], []
    run_width = 0
    for word, conf, left, width in zip(words, confs, lefts, widths):
        # A confident word always votes on its own — adjacency to garbage
        # must not silence it. Glued runs additionally catch amounts the
        # tokenizer split across touching boxes.
        if conf >= floor:
            amounts_out.extend(_parse(word))
        gap = left - run_width if run else 0
        if run and gap > max(2, int(0.3 * width)):
            if len(run) > 1 and all(c >= floor for c in run_confs):
                amounts_out.extend(_parse("".join(run)))
            run, run_confs = [], []
        run.append(word)
        run_confs.append(conf)
        run_width = left + width
    if len(run) > 1 and all(c >= floor for c in run_confs):
        amounts_out.extend(_parse("".join(run)))
    seen, deduped = set(), []
    for value in amounts_out:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _ocr_image(
    image: Image.Image, word_floor: float = 60.0
) -> tuple[str, float, list[float]]:
    """OCR one rendered page from Tesseract's word boxes: layout-aware text,
    line-weighted confidence, and the numbers read confidently enough to
    second-guess the vision model.

    One image_to_data pass feeds everything — text, confidence gate, and
    witness — so the three can never disagree about what Tesseract saw.
    The word-level floor keeps a garbled witness silent: a number Tesseract
    itself is unsure about can neither confirm nor accuse.
    """
    data = pytesseract.image_to_data(
        image,
        lang=config.OCR_LANG,
        config=f"--psm {config.OCR_PSM}",
        output_type=pytesseract.Output.DICT,
    )
    n = len(data.get("text", []))
    lines: dict[tuple, list[int]] = {}
    for i in range(n):
        key = (
            data["page_num"][i],
            data["block_num"][i],
            data["par_num"][i],
            data["line_num"][i],
        )
        lines.setdefault(key, []).append(i)

    texts, line_confs = [], []
    confident: list[float] = []
    for key in sorted(lines):
        words, confs, lefts, widths = _line_words(data, lines[key])
        if not words:
            continue
        texts.append(_serialize_line(words, lefts, widths))
        avg = sum(confs) / len(confs)
        line_confs.append((avg, sum(len(w) for w in words)))
        confident.extend(_glued_amounts(words, confs, lefts, widths, word_floor))

    text = "\n".join(texts)
    total_chars = sum(chars for _, chars in line_confs)
    good_chars = sum(chars for avg, chars in line_confs if avg >= word_floor)
    confidence = 100.0 * good_chars / total_chars if total_chars else 0.0
    return text, confidence, confident


def _pdf_table_block(page) -> str:
    """Serialize a PDF page's tables as markdown rows, verbatim cell text.

    Pure geometry from pdfplumber's line/word detection — no keywords, no
    per-vendor logic. Gives the extractor column boundaries that a flat
    text dump loses, so quantities stop sliding into neighbouring columns.
    Never raises: a page without detectable tables contributes nothing.
    """
    try:
        tables = page.extract_tables() or []
    except Exception:
        return ""
    blocks = []
    for table in tables:
        rows = [
            "| " + " | ".join((cell or "").strip() for cell in row) + " |"
            for row in table
            if any((cell or "").strip() for cell in row)
        ]
        if rows:
            blocks.append("\n".join(rows))
    return "\n".join(blocks)


def extract_text(
    path: str | Path, *, ocr_confidence_floor: float = 60.0, force_vlm: bool = False
) -> OcrResult:
    """Get the best available text for a document.

    Order of attempts: PDF text layer -> Tesseract OCR -> VLM transcription.
    Each stage only runs if the previous one didn't produce trustworthy text.

    `force_vlm` skips straight to the vision model. It's what the pipeline
    uses to re-read a document whose OCR text passed the confidence gate but
    then failed validation — see pipeline.process.
    """
    path = Path(path)
    if force_vlm:
        ocr_confidence_floor = float("inf")

    if path.suffix.lower() == ".pdf":
        with pdfplumber.open(path) as pdf:
            pdf_pages = list(pdf.pages)
            page_texts = [page.extract_text() or "" for page in pdf_pages]
            table_blocks = [_pdf_table_block(page) for page in pdf_pages]
        if len(page_texts) > config.MAX_PDF_PAGES:
            raise ValueError(
                f"PDF has {len(page_texts)} pages; limit is {config.MAX_PDF_PAGES}"
            )

        resolved_pages: list[str] = []
        methods: list[str] = []
        witnesses: list[str | None] = []
        witness_amounts: list[list[float]] = []
        for page_number, text_layer in enumerate(page_texts):
            table = table_blocks[page_number]
            if not force_vlm and len(text_layer.strip()) >= config.MIN_CHARS_PER_PAGE:
                page_text = text_layer
                if table and table not in page_text:
                    page_text += "\n[TABLES]\n" + table
                resolved_pages.append(page_text)
                methods.append("pdf_text")
                witnesses.append(None)
                witness_amounts.append([])
                continue

            image = _render_pdf_page(path, page_number)
            ocr_text, confidence, confident = _ocr_image(image)
            if (
                not force_vlm
                and confidence >= ocr_confidence_floor
                and ocr_text.strip()
            ):
                resolved_pages.append(ocr_text)
                methods.append("ocr")
                witnesses.append(None)
                witness_amounts.append([])
                continue

            tmp_path = path.with_suffix(f".page{page_number + 1}.tmp.png")
            try:
                image.save(tmp_path)
                page_result = _vlm_or_degraded_ocr([tmp_path], ocr_text, [confident])
            finally:
                tmp_path.unlink(missing_ok=True)
            resolved_pages.append(page_result.text)
            methods.append(page_result.method)
            witnesses.append(ocr_text if ocr_text.strip() else None)
            witness_amounts.append(confident)

        combined = "\n".join(
            f"[PAGE {i}]\n{text}" for i, text in enumerate(resolved_pages, start=1)
        )
        method = methods[0] if methods and len(set(methods)) == 1 else "mixed"
        return OcrResult(
            text=combined,
            method=method,
            pages=resolved_pages,
            page_methods=methods,
            witness_pages=witnesses,
            witness_numbers=witness_amounts,
        )

    if path.suffix.lower() in IMAGE_SUFFIXES:
        image = Image.open(path)
        text, conf, confident = _ocr_image(image)
        if conf >= ocr_confidence_floor and len(text.strip()) > 0:
            return OcrResult(
                text=text, method="ocr", pages=[text], page_methods=["ocr"]
            )
        return _vlm_or_degraded_ocr([path], text, [confident])

    if path.suffix.lower() in {".txt", ".md"}:
        pages = path.read_text().split("\f")
        text = "\n".join(f"[PAGE {i}]\n{page}" for i, page in enumerate(pages, 1))
        return OcrResult(
            text=text,
            method="pdf_text",
            pages=pages,
            page_methods=["pdf_text"] * len(pages),
        )

    raise ValueError(f"Unsupported file type: {path.suffix}")
