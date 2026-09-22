from PIL import Image, ImageDraw
import pytest

from docket.ocr.deskew import deskew_image


def test_projection_deskew_corrects_a_small_angle():
    image = Image.new("RGB", (700, 350), "white")
    draw = ImageDraw.Draw(image)
    for y in range(60, 300, 35):
        draw.rectangle((80, y, 620, y + 8), fill="black")
    skewed = image.rotate(2.5, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")
    corrected, angle = deskew_image(skewed)
    assert corrected.size == skewed.size
    assert angle == pytest.approx(2.5, abs=0.6)
