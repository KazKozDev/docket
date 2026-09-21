"""Document forensics: stamps, seals, signatures and handwritten alterations.

This is a pixel heuristic, not a trained vision model. It needs no extra
dependencies beyond Pillow and Tesseract, and it is honest about what it can
and cannot see:

- Colored ink (blue, violet, red) is separated from black print by hue, then
  grouped into clusters and classified by geometry: round-ish clusters are
  stamps/seals, elongated ones are handwriting.
- Dark (black/grey) ink is only considered where Tesseract did *not*
  recognise printed words, and only in the signing zone (lower part of the
  page or next to a "Signature / Unterschrift / Firma" label). Elsewhere a
  black logo, chart or photo looks the same as a black stamp to this method,
  so dark marks in the body are not reported.
- A status stamp (PAID, BEZAHLT, PAYÉ, PAGADO, VOID, ОПЛАЧЕНО, ...) is
  reported only when the word is read *inside* a detected stamp, so "Amount
  paid" printed on a receipt is not mistaken for one.
- Handwritten corrections are found by marker words ("corrected",
  "korrigiert", "исправлено", ...), which needs the matching Tesseract
  language packs (`DOCKET_OCR_LANGUAGES`).

Confidence values are heuristic scores derived from geometry and position.
They are useful for ranking and thresholds, not calibrated probabilities.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytesseract
from PIL import Image

from . import config, pdf as pdf_render
from .ocr.languages import parse_languages, tesseract_codes
from .schemas import (
    DocumentForensicReport,
    HandwrittenAnnotation,
    SignatureDetection,
    StampDetection,
)

_PAYMENT_WORDS = (
    r"paid|bezahlt|payé|paye|pagado|pagato|betaald|zapłacono|zaplacono|pago|"
    r"received|erhalten|reçu|recu|recibido|ricevuto|ontvangen|gebucht|"
    r"оплачено|получено|к\s*оплате"
)
_APPROVAL_WORDS = (
    r"approved|genehmigt|freigegeben|approuvé|approuve|aprobado|approvato|"
    r"goedgekeurd|zatwierdzono|согласовано|копия\s*верна"
)
_VOID_WORDS = (
    r"void|voided|cancelled|canceled|storniert|annulé|annule|anulado|annullato|"
    r"geannuleerd|аннулировано"
)
PAYMENT_STAMP_REGEX = re.compile(rf"(?i)\b({_PAYMENT_WORDS})\b")
APPROVAL_STAMP_REGEX = re.compile(rf"(?i)\b({_APPROVAL_WORDS})\b")
VOID_STAMP_REGEX = re.compile(rf"(?i)\b({_VOID_WORDS})\b")
ALTERATION_REGEX = re.compile(
    r"(?i)\b(исправленному\s*верить|исправлено|пересчитано|correction|corrected|"
    r"korrigiert|berichtigt|korrektur|corrigé|corrige|corregido|corrección|correccion|"
    r"corretto|correzione|gecorrigeerd|poprawiono|korekta)\b"
)
SIGNATURE_LABEL_REGEX = re.compile(
    r"(?i)(signature|signed|signatory|firma|firmado|unterschrift|signé|signe|"
    r"handtekening|podpis|подпись)"
)

_SCAN_MAX_DIM = 800
_CELL = 16
# Lower part of the page, where signatures and seals usually sit.
_SIGNING_ZONE_TOP = 0.45
# Tesseract confidence above which a word counts as machine print. Small or
# underlined print often scores 30-60; handwriting read as text scores lower.
_PRINTED_WORD_CONF = 30


@dataclass(frozen=True)
class _Word:
    text: str
    conf: float
    box: tuple[float, float, float, float]  # normalized (ymin, xmin, ymax, xmax)
    line: tuple[int, int, int]


def _load_page_images(doc_path: str | Path) -> list[Image.Image]:
    """Extract PIL RGB Images for each page of a PDF or image file."""
    path = Path(doc_path)
    if path.suffix.lower() == ".pdf":
        # 150 DPI provides sharp ink detail with fast processing speed
        return pdf_render.render_pages(path, dpi=150)
    return [Image.open(str(path)).convert("RGB")]


def _tesseract_lang() -> str | None:
    try:
        return "+".join(tesseract_codes(parse_languages(config.OCR_LANGUAGES)))
    except ValueError:
        return None


def _ocr_words(image: Image.Image) -> list[_Word] | None:
    """Every word Tesseract finds, with its box. None if OCR is unavailable."""
    data = None
    for lang in (_tesseract_lang(), None):
        try:
            kwargs = {"lang": lang} if lang else {}
            data = pytesseract.image_to_data(
                image, output_type=pytesseract.Output.DICT, **kwargs
            )
            break
        except Exception:  # noqa: BLE001 — missing language pack or tesseract
            continue
    if data is None:
        return None
    w, h = image.size
    words = []
    for i, text in enumerate(data.get("text", [])):
        text = (text or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        left, top = data["left"][i], data["top"][i]
        words.append(
            _Word(
                text=text,
                conf=conf,
                box=(
                    top / h,
                    left / w,
                    (top + data["height"][i]) / h,
                    (left + data["width"][i]) / w,
                ),
                line=(data["block_num"][i], data["par_num"][i], data["line_num"][i]),
            )
        )
    return words


def _is_printed(word: _Word) -> bool:
    if SIGNATURE_LABEL_REGEX.search(word.text) or set(word.text) <= set("_.-"):
        # Signature labels and "______" lines are print however low Tesseract
        # scores them (an underline glued to a label drags it down to ~5).
        return True
    return word.conf >= _PRINTED_WORD_CONF and any(ch.isalnum() for ch in word.text)


def _page_lines(words: list[_Word]) -> list[str]:
    lines: dict[tuple[int, int, int], list[str]] = {}
    for word in words:
        lines.setdefault(word.line, []).append(word.text)
    return [" ".join(parts) for parts in lines.values()]


def _ink_clusters(
    image: Image.Image, printed_boxes: list[tuple[float, float, float, float]] | None
) -> dict[str, list[dict]]:
    """Group ink into clusters per color: "blue", "red", "violet", and — when
    printed word boxes are known — "dark" ink that isn't part of any printed word.
    """
    w, h = image.size
    if max(w, h) > _SCAN_MAX_DIM:
        scale = _SCAN_MAX_DIM / max(w, h)
        sw, sh = int(w * scale), int(h * scale)
        scan_img = image.resize((sw, sh), Image.Resampling.BILINEAR)
    else:
        sw, sh = w, h
        scan_img = image
    pixels = scan_img.load()
    if pixels is None:
        return {}

    detect_dark = printed_boxes is not None
    mask = bytearray(sw * sh) if detect_dark else None
    if mask is not None:
        pad = 2
        for ymin, xmin, ymax, xmax in printed_boxes or []:
            x0 = max(0, int(xmin * sw) - pad)
            x1 = min(sw, int(xmax * sw) + pad)
            y0 = max(0, int(ymin * sh) - pad)
            y1 = min(sh, int(ymax * sh) + pad)
            if x1 <= x0:
                continue
            ones = b"\x01" * (x1 - x0)
            for row in range(y0, y1):
                mask[row * sw + x0 : row * sw + x1] = ones

    cols = (sw + _CELL - 1) // _CELL
    rows = (sh + _CELL - 1) // _CELL
    grids = {name: [[0] * cols for _ in range(rows)] for name in ("blue", "red", "violet", "dark")}
    blue, red, violet, dark = grids["blue"], grids["red"], grids["violet"], grids["dark"]
    darkmap = bytearray(sw * sh) if detect_dark else None

    for y in range(sh):
        gy = y // _CELL
        row_offset = y * sw
        for x in range(sw):
            r, g, b = pixels[x, y][:3]
            lum = r + g + b
            if lum > 690:
                continue  # paper
            gx = x // _CELL
            if max(r, g, b) - min(r, g, b) < 45:
                # Achromatic: print, pencil or black ink.
                if darkmap is not None and lum < 360:
                    darkmap[row_offset + x] = 1
                continue
            if lum < 75:
                continue
            if b > r + 22 and b > g + 18:
                blue[gy][gx] += 1
            elif r > g + 35 and r > b + 35:
                red[gy][gx] += 1
            elif r > 80 and b > 80 and g < r - 20 and g < b - 20:
                violet[gy][gx] += 1

    ink = None
    if darkmap is not None and mask is not None:
        ink = _handwriting_candidates(darkmap, mask, sw, sh)
        for y in range(sh):
            gy = y // _CELL
            for run in _RUN.finditer(ink, y * sw, (y + 1) * sw):
                for x in range(run.start() - y * sw, run.end() - y * sw):
                    dark[gy][x // _CELL] += 1

    result = {
        name: _connected_clusters(grid, sw, sh)
        for name, grid in grids.items()
        if name != "dark" or detect_dark
    }
    # Cells are 16px, so a line of print straddling a cell boundary looks two
    # cells tall. Measure the real ink height, which is what separates a
    # signature from print.
    for cl in result.get("dark", []):
        ymin, xmin, ymax, xmax = cl["box"]
        top, bottom = sh, -1
        x0, x1 = int(xmin * sw), min(sw, int(xmax * sw) + 1)
        y0, y1 = int(ymin * sh), min(sh, int(ymax * sh) + 1)
        total = straight = 0
        for y in range(y0, y1):
            row = ink[y * sw + x0 : y * sw + x1] if ink is not None else b""
            if 1 in row:
                top, bottom = min(top, y), max(bottom, y)
                total += row.count(1)
                straight += sum(r.end() - r.start() for r in _STRAIGHT.finditer(row))
        for x in range(x0, x1):
            column = bytes(ink[y0 * sw + x : y1 * sw : sw]) if ink is not None else b""
            straight += sum(r.end() - r.start() for r in _STRAIGHT.finditer(column))
        cl["ink_height"] = max(0, bottom - top + 1) / sh
        # Share of ink lying on short straight horizontal/vertical runs: high
        # for boxes, arrows and print fragments, low for pen strokes.
        cl["straightness"] = straight / total if total else 1.0
    return result


_RUN = re.compile(rb"\x01+")
_STRAIGHT = re.compile(rb"\x01{10,}")


def _or3(a: bytes, b: bytes, c: bytes) -> bytes:
    n = len(b)
    value = int.from_bytes(a, "big") | int.from_bytes(b, "big") | int.from_bytes(c, "big")
    return value.to_bytes(n, "big")


def _handwriting_candidates(darkmap: bytearray, mask: bytearray, sw: int, sh: int) -> bytearray:
    """Dark pixels that are neither printed words nor ruled lines.

    Table rules and signature lines are long straight runs; a pen stroke
    almost never stays within a 3-pixel band for a tenth of the page.
    Removing them keeps handwriting that crosses a table from merging with
    the grid into one page-sized blob. Runs are looked for in a band of three
    rows/columns so that a slightly skewed scan still shows a rule as one run.
    """
    lines = bytearray(sw * sh)
    long_h = re.compile(rb"\x01{%d,}" % max(20, sw // 10))
    long_v = re.compile(rb"\x01{%d,}" % max(20, sh // 10))
    rows = [bytes(darkmap[y * sw : (y + 1) * sw]) for y in range(sh)]
    empty_row = bytes(sw)
    for y in range(sh):
        band = _or3(rows[y - 1] if y else empty_row, rows[y], rows[y + 1] if y + 1 < sh else empty_row)
        for run in long_h.finditer(band):
            for yy in (y - 1, y, y + 1):
                if 0 <= yy < sh:
                    lines[yy * sw + run.start() : yy * sw + run.end()] = b"\x01" * (run.end() - run.start())
    columns = [bytes(darkmap[x::sw]) for x in range(sw)]
    empty_col = bytes(sh)
    for x in range(sw):
        band = _or3(columns[x - 1] if x else empty_col, columns[x], columns[x + 1] if x + 1 < sw else empty_col)
        for run in long_v.finditer(band):
            for xx in (x - 1, x, x + 1):
                if 0 <= xx < sw:
                    for y in range(run.start(), run.end()):
                        lines[y * sw + xx] = 1
    return bytearray(
        1 if d and not m and not ln else 0 for d, m, ln in zip(darkmap, mask, lines)
    )


def _connected_clusters(grid: list[list[int]], sw: int, sh: int, min_density: int = 6) -> list[dict]:
    rows, cols = len(grid), len(grid[0]) if grid else 0
    visited = [[False] * cols for _ in range(rows)]
    clusters = []
    for r in range(rows):
        for c in range(cols):
            if grid[r][c] < min_density or visited[r][c]:
                continue
            queue = [(r, c)]
            visited[r][c] = True
            cells = []
            total_ink = 0
            while queue:
                cr, cc = queue.pop()
                cells.append((cr, cc))
                total_ink += grid[cr][cc]
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        nr, nc = cr + dr, cc + dc
                        if (
                            0 <= nr < rows
                            and 0 <= nc < cols
                            and not visited[nr][nc]
                            and grid[nr][nc] >= min_density
                        ):
                            visited[nr][nc] = True
                            queue.append((nr, nc))
            if len(cells) < 3 or total_ink < 25:
                continue  # noise specks
            min_r = min(cell[0] for cell in cells)
            max_r = max(cell[0] for cell in cells)
            min_c = min(cell[1] for cell in cells)
            max_c = max(cell[1] for cell in cells)
            ymin, xmin = (min_r * _CELL) / sh, (min_c * _CELL) / sw
            ymax = min(1.0, ((max_r + 1) * _CELL) / sh)
            xmax = min(1.0, ((max_c + 1) * _CELL) / sw)
            clusters.append(
                {
                    "box": (ymin, xmin, ymax, xmax),
                    "total_ink": total_ink,
                    "num_cells": len(cells),
                    "rows": max_r - min_r + 1,
                    "width": xmax - xmin,
                    "height": ymax - ymin,
                    "pixel_width": (max_c - min_c + 1) * _CELL,
                    "pixel_height": (max_r - min_r + 1) * _CELL,
                }
            )
    return clusters


def _detect_colored_clusters(
    image: Image.Image,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Blue, red and violet ink clusters (kept for callers of the old API)."""
    clusters = _ink_clusters(image, None)
    return clusters.get("blue", []), clusters.get("red", []), clusters.get("violet", [])


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def _roundness(cluster: dict) -> float:
    aspect = cluster["pixel_width"] / max(1, cluster["pixel_height"])
    return _clip(1 - abs(math.log(aspect)) / math.log(2))


def _stamp_confidence(cluster: dict, *, dark: bool) -> float:
    # A seal is round-ish and a few percent of the page wide or more.
    size = _clip((cluster["width"] - 0.03) / 0.07)
    score = 0.35 + 0.35 * _roundness(cluster) + 0.25 * size
    return round(min(0.95, score * (0.8 if dark else 1.0)), 2)


def _signature_confidence(cluster: dict, *, labelled: bool, dark: bool) -> float:
    aspect = cluster["pixel_width"] / max(1, cluster["pixel_height"])
    elongation = _clip((aspect - 1) / 2)
    zone = 1.0 if labelled else (0.6 if cluster["box"][0] >= _SIGNING_ZONE_TOP else 0.2)
    score = 0.3 + 0.3 * elongation + 0.3 * zone
    return round(min(0.9, score * (0.85 if dark else 1.0)), 2)


def _near_label(cluster: dict, labels: list[_Word]) -> bool:
    ymin, _, ymax, _ = cluster["box"]
    center = (ymin + ymax) / 2
    return any(abs(center - (lb.box[0] + lb.box[2]) / 2) <= 0.08 for lb in labels)


def _is_stamp_shaped(cluster: dict) -> bool:
    aspect = cluster["pixel_width"] / max(1, cluster["pixel_height"])
    area = cluster["width"] * cluster["height"]
    return 0.70 <= aspect <= 1.40 and (area >= 0.002 or cluster["total_ink"] >= 35)


def _words_inside(box: tuple[float, float, float, float], words: list[_Word]) -> list[_Word]:
    ymin, xmin, ymax, xmax = box
    my, mx = (ymax - ymin) * 0.1, (xmax - xmin) * 0.1
    inside = []
    for word in words:
        cy = (word.box[0] + word.box[2]) / 2
        cx = (word.box[1] + word.box[3]) / 2
        if ymin - my <= cy <= ymax + my and xmin - mx <= cx <= xmax + mx:
            inside.append(word)
    return inside


def _status_annotations(text: str, page_num: int, box=None) -> list[HandwrittenAnnotation]:
    found = []
    for regex, kind in (
        (PAYMENT_STAMP_REGEX, "payment_stamp"),
        (APPROVAL_STAMP_REGEX, "approval_stamp"),
        (VOID_STAMP_REGEX, "void_stamp"),
    ):
        match = regex.search(text)
        if match:
            found.append(
                HandwrittenAnnotation(
                    page=page_num, text=match.group(0), annotation_type=kind, box=box
                )
            )
    return found


def analyze_page_forensics(
    image: Image.Image,
    page_num: int = 1,
    page_ocr_text: str | None = None,
) -> tuple[list[StampDetection], list[SignatureDetection], list[HandwrittenAnnotation]]:
    """Analyze a single page image for stamps, signatures, and annotations.

    `page_ocr_text`, if given, is searched for correction markers in addition
    to the text this function reads itself; word boxes always come from a
    fresh Tesseract pass, because locating stamps and print needs them.
    """
    stamps: list[StampDetection] = []
    signatures: list[SignatureDetection] = []
    annotations: list[HandwrittenAnnotation] = []

    words = _ocr_words(image)
    printed = [w.box for w in words if _is_printed(w)] if words is not None else None
    heights = sorted(
        w.box[2] - w.box[0] for w in words or [] if w.conf >= 60 and w.box[2] > w.box[0]
    )
    text_height = heights[len(heights) // 2] if heights else 0.015
    labels = [w for w in words or [] if SIGNATURE_LABEL_REGEX.search(w.text)]
    clusters = _ink_clusters(image, printed)

    # 1. Colored ink: stamps by shape, handwriting otherwise.
    for color in ("red", "blue", "violet"):
        for cl in clusters.get(color, []):
            box = cl["box"]
            aspect = cl["pixel_width"] / max(1, cl["pixel_height"])
            if color == "red" or _is_stamp_shaped(cl):
                # Red ink on business paper is nearly always a stamp, round or not.
                if _is_stamp_shaped(cl):
                    shape = "circular" if 0.85 <= aspect <= 1.15 else "oval"
                else:
                    shape = "rectangular"
                stamps.append(
                    StampDetection(
                        page=page_num,
                        box=box,
                        color=color,
                        shape=shape,
                        confidence=_stamp_confidence(cl, dark=False),
                    )
                )
                continue
            labelled = _near_label(cl, labels)
            if box[0] >= _SIGNING_ZONE_TOP or labelled:
                signatures.append(
                    SignatureDetection(
                        page=page_num,
                        box=box,
                        confidence=_signature_confidence(cl, labelled=labelled, dark=False),
                    )
                )
            else:
                annotations.append(
                    HandwrittenAnnotation(
                        page=page_num,
                        text="Handwritten note",
                        annotation_type="marginalia",
                        box=box,
                    )
                )

    # 2. Dark ink outside printed words, signing zone only (see module docstring).
    for cl in clusters.get("dark", []):
        box = cl["box"]
        labelled = _near_label(cl, labels)
        if not (box[0] >= _SIGNING_ZONE_TOP or labelled):
            continue
        if cl["ink_height"] < 2 * text_height or cl["width"] > 0.45 or cl["height"] > 0.3:
            continue  # print, a signature line, a table rule or a border
        if box[1] < 0.06 or box[3] > 0.94:
            continue  # page edge: punch holes, scanner shadow, rotated file numbers
        if cl["pixel_width"] < cl["pixel_height"] * 0.8:
            continue  # much taller than wide: rotated print or a scanner artifact
        if cl["straightness"] > 0.5:
            continue  # mostly straight segments: a box, arrow or graphic
        # Black ink is only ever reported as handwriting. A black seal looks
        # like a logo, a table cell or a chart to this method, so no black
        # stamps are claimed at all.
        signatures.append(
            SignatureDetection(
                page=page_num,
                box=box,
                confidence=_signature_confidence(cl, labelled=labelled, dark=True),
            )
        )

    # 3. Status words read inside a detected stamp.
    if words is not None:
        for stamp in stamps:
            inside = " ".join(w.text for w in _words_inside(stamp.box, words))
            annotations.extend(_status_annotations(inside, page_num, stamp.box))
    elif stamps and page_ocr_text:
        # No word boxes: fall back to page text, but only when a stamp exists.
        annotations.extend(_status_annotations(page_ocr_text, page_num))

    # 4. Correction markers anywhere in the text.
    lines = _page_lines(words or [])
    if page_ocr_text:
        lines += page_ocr_text.splitlines()
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if line and line not in seen and ALTERATION_REGEX.search(line):
            seen.add(line)
            annotations.append(
                HandwrittenAnnotation(
                    page=page_num, text=line, annotation_type="price_correction"
                )
            )

    return stamps, signatures, annotations


def analyze_document_forensics(
    doc_path: str | Path,
    ocr_texts_per_page: Sequence[str] | None = None,
) -> DocumentForensicReport:
    """Analyze an entire multi-page document (PDF or image) for physical execution.

    Args:
        doc_path: Path to PDF or image file.
        ocr_texts_per_page: Optional list of pre-extracted OCR text per page.

    Returns:
        Structured DocumentForensicReport with stamp, signature, and template verification.
    """
    images = _load_page_images(doc_path)

    all_stamps: list[StampDetection] = []
    all_signatures: list[SignatureDetection] = []
    all_annotations: list[HandwrittenAnnotation] = []

    for idx, img in enumerate(images, start=1):
        ocr_text = (
            ocr_texts_per_page[idx - 1]
            if ocr_texts_per_page and idx - 1 < len(ocr_texts_per_page)
            else None
        )
        stamps, signatures, annotations = analyze_page_forensics(
            img, page_num=idx, page_ocr_text=ocr_text
        )
        all_stamps.extend(stamps)
        all_signatures.extend(signatures)
        all_annotations.extend(annotations)

    has_sigs = len(all_signatures) > 0
    has_stamps = len(all_stamps) > 0
    is_executed = has_sigs or has_stamps
    is_empty_template = not has_sigs and not has_stamps

    alterations_detected = any(
        a.annotation_type == "price_correction" for a in all_annotations
    )

    risk_flags: list[str] = []
    if is_empty_template:
        risk_flags.append("UNEXECUTED_TEMPLATE")
    else:
        if not has_sigs:
            risk_flags.append("MISSING_SIGNATURE")
        if not has_stamps:
            risk_flags.append("MISSING_STAMP")

    if any(a.annotation_type == "payment_stamp" for a in all_annotations):
        risk_flags.append("PAYMENT_STAMP_PRESENT")
    if any(a.annotation_type == "void_stamp" for a in all_annotations):
        risk_flags.append("VOID_STAMP_PRESENT")

    if alterations_detected:
        risk_flags.append("HANDWRITTEN_ALTERATION")

    return DocumentForensicReport(
        has_signatures=has_sigs,
        has_stamps=has_stamps,
        is_executed=is_executed,
        is_empty_template=is_empty_template,
        stamps=all_stamps,
        signatures=all_signatures,
        annotations=all_annotations,
        alterations_detected=alterations_detected,
        risk_flags=risk_flags,
    )
