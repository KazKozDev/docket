"""Small, dependency-free fine-angle deskew for OCR page images."""
from __future__ import annotations

from typing import cast

from PIL import Image, ImageOps


def _projection_score(image: Image.Image, angle: float) -> float:
    rotated = image.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False, fillcolor=255)
    pixels = rotated.load()
    assert pixels is not None  # a loaded grayscale image
    width, height = rotated.size
    rows = [sum(255 - cast(int, pixels[x, y]) for x in range(width)) for y in range(height)]
    return sum((rows[i] - rows[i - 1]) ** 2 for i in range(1, height))


def deskew_image(image: Image.Image, *, max_angle: float = 5.0) -> tuple[Image.Image, float]:
    """Return a corrected image and clockwise degrees applied.

    Horizontal text produces sharp peaks in a row projection. Searching a
    thumbnail keeps the operation cheap and avoids an OpenCV dependency.
    """
    gray = ImageOps.grayscale(image)
    gray.thumbnail((900, 900))
    histogram = gray.histogram()
    if sum(count > 0 for count in histogram) == 1:
        return image, 0.0
    dark = sum(histogram[:200])
    if dark < max(20, gray.width * gray.height // 1000):
        return image, 0.0

    coarse = [n / 2 for n in range(int(-2 * max_angle), int(2 * max_angle) + 1)]
    best = max(coarse, key=lambda angle: _projection_score(gray, angle))
    fine = [best + n / 10 for n in range(-4, 5) if abs(best + n / 10) <= max_angle]
    best = max(fine, key=lambda angle: _projection_score(gray, angle))
    # Below 1.5 degrees resampling often costs OCR detail without changing
    # line grouping; leave those pages untouched.
    if abs(best) < 1.5:
        return image, 0.0
    corrected = image.rotate(best, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")
    return corrected, round(-best, 2)


__all__ = ["deskew_image"]
