"""Page image preparation before OCR: photo cropping and small-text upscaling.

A phone photo of a receipt or invoice holds the document somewhere in a
frame of table, hand and shadow, at an angle. OCR reads the table grain as
characters and the tilted lines as a slope. `crop_document` finds the
paper's four corners and warps it flat. It needs OpenCV (the `[photo]`
extra); without it photos are read as they are.

Tesseract reads text best when capital letters are 20–30 pixels tall; a
low-resolution scan gives it 10. `upscale_factor` says how far to enlarge a
page whose words came out that small (see TesseractBackend).

Every step here is conservative: when in doubt it leaves the image alone,
because a wrong crop loses text and nothing downstream can bring it back.
"""
from __future__ import annotations

from collections.abc import Callable
from statistics import median

from PIL import Image

# Page image, page number -> page image. Runs on every rendered PDF page and
# image page before any OCR engine sees it (Docling reads the file itself).
Preprocessor = Callable[[Image.Image, int], Image.Image]

# Four corners (top-left, top-right, bottom-right, bottom-left) of the
# document in the original image, normalized to 0..1.
Quad = list[tuple[float, float]]

# A crop must keep at least this share of the frame (smaller is more likely
# a label, a table or a card than the page), and leave out at least this
# much (otherwise the page already fills the frame: a scan).
_MIN_AREA, _MAX_AREA = 0.20, 0.92
# The frame outside the page must be background, not more paper: the page
# must be clearly brighter than what surrounds it (median grey levels), and
# the surroundings must not be busy with edges. A table frame on a scanned
# page fails the first test — white paper on both sides of the line.
_MIN_PAPER_CONTRAST = 25
_MAX_OUTSIDE_EDGES = 0.02
# Work on a copy this wide; corner finding needs shape, not detail.
_DETECT_WIDTH = 800


def crop_document(image: Image.Image) -> tuple[Image.Image, Quad | None]:
    """The document flattened out of a photo, and where it was; or the image
    unchanged and None when no page outline is clear enough to trust."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return image, None

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    scale = min(1.0, _DETECT_WIDTH / width)
    small = cv2.resize(rgb, (round(width * scale), round(height * scale))) if scale < 1 else rgb
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    closed = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    frame = float(small.shape[0] * small.shape[1])

    quad = None
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            share = cv2.contourArea(approx) / frame
            if _MIN_AREA <= share <= _MAX_AREA:
                quad = approx.reshape(4, 2).astype("float32")
            break  # only the largest outline can be the page
    if quad is None:
        return image, None

    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillConvexPoly(mask, quad.astype(np.int32), (255,))
    # Leave the outline's own edge band out of the "outside".
    inside = cv2.dilate(mask, np.ones((9, 9), np.uint8))
    outside = inside == 0
    if not outside.any():
        return image, None
    contrast = float(np.median(gray[mask > 0])) - float(np.median(gray[outside]))
    if contrast < _MIN_PAPER_CONTRAST or (edges[outside] > 0).mean() > _MAX_OUTSIDE_EDGES:
        return image, None

    corners = _ordered(quad / scale)
    top = np.linalg.norm(corners[1] - corners[0])
    bottom = np.linalg.norm(corners[2] - corners[3])
    left = np.linalg.norm(corners[3] - corners[0])
    right = np.linalg.norm(corners[2] - corners[1])
    out_w, out_h = round(max(top, bottom)), round(max(left, right))
    target = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype="float32")
    warp = cv2.getPerspectiveTransform(corners.astype("float32"), target)
    flat = cv2.warpPerspective(rgb, warp, (out_w, out_h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
    normalized = [(float(x) / width, float(y) / height) for x, y in corners]
    return Image.fromarray(flat), normalized


def _ordered(points):
    """Corners as top-left, top-right, bottom-right, bottom-left."""
    import numpy as np

    by_sum = points.sum(axis=1)
    by_diff = np.diff(points, axis=1).ravel()
    return np.array([
        points[by_sum.argmin()], points[by_diff.argmin()], points[by_sum.argmax()], points[by_diff.argmax()],
    ])


def upscale_factor(word_heights: list[float], *, min_height: float, target: float = 30.0,
                   max_factor: float = 3.0) -> float:
    """How much to enlarge a page whose typical word is `word_heights` tall:
    1.0 when it is at least `min_height` px (or nothing was read), else
    enough to reach `target`, at most `max_factor`."""
    if min_height <= 0 or not word_heights:
        return 1.0
    typical = median(word_heights)
    if typical <= 0 or typical >= min_height:
        return 1.0
    return min(max_factor, target / typical)


__all__ = ["Preprocessor", "Quad", "crop_document", "upscale_factor"]
