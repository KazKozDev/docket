"""Computer Vision and Document Forensics module.

Detects:
- Physical seals and organization stamps (color masking, circularity, geometry)
- Handwritten signatures and endorsements in signatory zones
- Blank / unexecuted contract and act templates (UNEXECUTED_TEMPLATE)
- Status stamps ("ОПЛАЧЕНО", "PAID", "ПОЛУЧЕНО", "VOID")
- Handwritten numeric alterations and corrections
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

import pytesseract
from PIL import Image

from . import config, pdf as pdf_render
from .schemas import (
    DocumentForensicReport,
    HandwrittenAnnotation,
    SignatureDetection,
    StampDetection,
)

# Common stamp and annotation regex patterns
PAYMENT_STAMP_REGEX = re.compile(
    r"(?i)\b(оплачено|paid|получено|received|к\s*оплате|аннулировано|void|cancelled|копия\s*верна|согласовано|approved)\b"
)
ALTERATION_REGEX = re.compile(
    r"(?i)\b(исправленному\s*верить|исправлено|пересчитано|correction|corrected|voided)\b"
)


def _load_page_images(doc_path: str | Path) -> list[Image.Image]:
    """Extract PIL RGB Images for each page of a PDF or image file."""
    path = Path(doc_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        # 150 DPI provides sharp ink detail with fast processing speed
        return pdf_render.render_pages(path, dpi=150)

    # Single image file
    img = Image.open(str(path)).convert("RGB")
    return [img]


def _detect_colored_clusters(
    image: Image.Image,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Detect colored ink clusters using chromatic separation.

    Separates:
    - Blue/Violet ink (traditional bank/corporate stamps and ballpoint signatures)
    - Red/Magenta ink (official audit, approval, or payment stamps)

    Returns:
        (blue_clusters, red_clusters, violet_clusters)
    """
    # Downsample for fast raster scanning if image is very large
    max_dim = 800
    w, h = image.size
    scale = 1.0
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        scaled_w, scaled_h = int(w * scale), int(h * scale)
        scan_img = image.resize((scaled_w, scaled_h), Image.Resampling.BILINEAR)
    else:
        scan_img = image
        scaled_w, scaled_h = w, h

    pixels = scan_img.load()
    if pixels is None:
        return [], [], []

    # Grid cell size (e.g. 16x16 pixels)
    cell_size = 16
    grid_cols = (scaled_w + cell_size - 1) // cell_size
    grid_rows = (scaled_h + cell_size - 1) // cell_size

    blue_grid = [[0] * grid_cols for _ in range(grid_rows)]
    red_grid = [[0] * grid_cols for _ in range(grid_rows)]
    violet_grid = [[0] * grid_cols for _ in range(grid_rows)]

    for y in range(scaled_h):
        gy = y // cell_size
        for x in range(scaled_w):
            r, g, b = pixels[x, y][:3]
            lum = r + g + b
            # Skip white background and dark monochrome print
            if lum > 690 or lum < 75:
                continue

            gx = x // cell_size
            # Blue ink: strong blue over red/green
            if b > r + 22 and b > g + 18:
                blue_grid[gy][gx] += 1
            # Red ink: strong red over green/blue
            elif r > g + 35 and r > b + 35:
                red_grid[gy][gx] += 1
            # Violet/Purple ink: both blue and red high, green low
            elif r > 80 and b > 80 and g < r - 20 and g < b - 20:
                violet_grid[gy][gx] += 1

    def find_connected_boxes(grid: list[list[int]], min_density: int = 6) -> list[dict]:
        visited = [[False] * grid_cols for _ in range(grid_rows)]
        clusters = []

        for r in range(grid_rows):
            for c in range(grid_cols):
                if grid[r][c] >= min_density and not visited[r][c]:
                    # BFS cluster
                    queue = [(r, c)]
                    visited[r][c] = True
                    cells = []
                    total_ink = 0

                    while queue:
                        cr, cc = queue.pop(0)
                        cells.append((cr, cc))
                        total_ink += grid[cr][cc]

                        for nr, nc in [
                            (cr - 1, cc),
                            (cr + 1, cc),
                            (cr, cc - 1),
                            (cr, cc + 1),
                            (cr - 1, cc - 1),
                            (cr - 1, cc + 1),
                            (cr + 1, cc - 1),
                            (cr + 1, cc + 1),
                        ]:
                            if (
                                0 <= nr < grid_rows
                                and 0 <= nc < grid_cols
                                and not visited[nr][nc]
                                and grid[nr][nc] >= min_density
                            ):
                                visited[nr][nc] = True
                                queue.append((nr, nc))

                    # Filter tiny noise specks
                    if len(cells) >= 3 and total_ink >= 25:
                        min_r = min(cell[0] for cell in cells)
                        max_r = max(cell[0] for cell in cells)
                        min_c = min(cell[1] for cell in cells)
                        max_c = max(cell[1] for cell in cells)

                        ymin = (min_r * cell_size) / scaled_h
                        ymax = min(1.0, ((max_r + 1) * cell_size) / scaled_h)
                        xmin = (min_c * cell_size) / scaled_w
                        xmax = min(1.0, ((max_c + 1) * cell_size) / scaled_w)

                        pixel_width = (max_c - min_c + 1) * cell_size
                        pixel_height = (max_r - min_r + 1) * cell_size

                        clusters.append(
                            {
                                "box": (ymin, xmin, ymax, xmax),
                                "total_ink": total_ink,
                                "num_cells": len(cells),
                                "width": xmax - xmin,
                                "height": ymax - ymin,
                                "pixel_width": pixel_width,
                                "pixel_height": pixel_height,
                            }
                        )

        return clusters

    return (
        find_connected_boxes(blue_grid),
        find_connected_boxes(red_grid),
        find_connected_boxes(violet_grid),
    )


def analyze_page_forensics(
    image: Image.Image,
    page_num: int = 1,
    page_ocr_text: str | None = None,
) -> tuple[list[StampDetection], list[SignatureDetection], list[HandwrittenAnnotation]]:
    """Analyze a single page image for stamps, signatures, and annotations."""
    stamps: list[StampDetection] = []
    signatures: list[SignatureDetection] = []
    annotations: list[HandwrittenAnnotation] = []

    blue_clusters, red_clusters, violet_clusters = _detect_colored_clusters(image)

    # 1. Process Red Clusters (approval, payment stamps, or alerts)
    for cl in red_clusters:
        box = cl["box"]
        p_aspect = cl["pixel_width"] / max(1, cl["pixel_height"])
        shape = "rectangular" if (p_aspect > 1.4 or p_aspect < 0.7) else "circular"
        stamps.append(
            StampDetection(
                page=page_num,
                box=box,
                color="red",
                shape=shape,
                confidence=0.92,
            )
        )
        # Red stamps are predominantly status/payment/approval stamps
        annotations.append(
            HandwrittenAnnotation(
                page=page_num,
                text="STAMP_RED",
                annotation_type="payment_stamp",
                box=box,
            )
        )

    # 2. Process Blue and Violet Clusters (Stamps vs Signatures)
    colored_clusters = [("blue", cl) for cl in blue_clusters] + [
        ("violet", cl) for cl in violet_clusters
    ]

    for color, cl in colored_clusters:
        box = cl["box"]
        p_aspect = cl["pixel_width"] / max(1, cl["pixel_height"])
        area = cl["width"] * cl["height"]

        # Circular or oval stamp:
        # In real documents, stamp seals have pixel aspect ratio close to 1.0 (0.70 to 1.40)
        # and balanced area
        if 0.70 <= p_aspect <= 1.40 and (area >= 0.002 or cl["total_ink"] >= 35):
            stamps.append(
                StampDetection(
                    page=page_num,
                    box=box,
                    color=color,
                    shape="circular" if 0.85 <= p_aspect <= 1.15 else "oval",
                    confidence=0.95,
                )
            )
        # Signatures: typically located in bottom signing region (ymin >= 0.45)
        # Colored handwriting strokes in upper/body area (ymin < 0.45) are marginalia or corrections
        elif p_aspect > 1.2 or (cl["box"][0] >= 0.45 and area >= 0.0008):
            if cl["box"][0] < 0.45:
                # Body annotation / correction
                annotations.append(
                    HandwrittenAnnotation(
                        page=page_num,
                        text="Handwritten annotation",
                        annotation_type="price_correction",
                        box=box,
                    )
                )
            else:
                signatures.append(
                    SignatureDetection(
                        page=page_num,
                        box=box,
                        confidence=0.90,
                    )
                )
        else:
            # Compact mark or initial
            if cl["box"][0] < 0.45:
                annotations.append(
                    HandwrittenAnnotation(
                        page=page_num,
                        text="Handwritten mark",
                        annotation_type="marginalia",
                        box=box,
                    )
                )
            else:
                signatures.append(
                    SignatureDetection(
                        page=page_num,
                        box=box,
                        confidence=0.80,
                    )
                )

    # 3. Check OCR text for payment stamps and handwritten alteration markers
    if page_ocr_text is None:
        try:
            page_ocr_text = pytesseract.image_to_string(image, lang=config.OCR_LANG)
        except Exception:
            try:
                page_ocr_text = pytesseract.image_to_string(image)
            except Exception:
                page_ocr_text = ""

    for line in page_ocr_text.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue

        match_payment = PAYMENT_STAMP_REGEX.search(line_clean)
        if match_payment:
            annotations.append(
                HandwrittenAnnotation(
                    page=page_num,
                    text=match_payment.group(0),
                    annotation_type="payment_stamp",
                )
            )

        match_alt = ALTERATION_REGEX.search(line_clean)
        if match_alt:
            annotations.append(
                HandwrittenAnnotation(
                    page=page_num,
                    text=line_clean,
                    annotation_type="price_correction",
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
