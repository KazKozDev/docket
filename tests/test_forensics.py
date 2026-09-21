"""Tests for Computer Vision and Document Forensics (stamps, signatures, and alterations)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from docket.forensics import (
    analyze_document_forensics,
    analyze_page_forensics,
)

FONT = ImageFont.load_default(size=16)


def _black_signed_contract(tmp_path: Path, *, with_seal: bool = False) -> Path:
    """A contract printed in black and signed in black ballpoint."""
    img = Image.new("RGB", (600, 800), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((50, 50), "SERVICE AGREEMENT 2026-07", fill=(0, 0, 0), font=FONT)
    draw.text((50, 100), "Customer: Global Corp GmbH", fill=(0, 0, 0), font=FONT)
    draw.text((50, 660), "Signature:", fill=(0, 0, 0), font=FONT)
    # Cursive strokes well taller than a line of print.
    draw.line(
        [(150, 680), (165, 640), (180, 690), (195, 645), (215, 685), (240, 640), (270, 675), (300, 660)],
        fill=(15, 15, 15),
        width=3,
    )
    if with_seal:
        draw.ellipse([400, 600, 520, 720], outline=(20, 20, 20), width=4)
        draw.ellipse([420, 620, 500, 700], outline=(20, 20, 20), width=2)
    path = tmp_path / "black_signed.png"
    img.save(path)
    return path


def _create_blank_contract_template(tmp_path: Path) -> Path:
    """Creates a black-and-white contract image without stamps or signatures."""
    img = Image.new("RGB", (600, 800), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Black printed text
    draw.text((50, 50), "SERVICE AGREEMENT # 2026-01", fill=(0, 0, 0))
    draw.text((50, 100), "Party A (Customer): Global Corp LLC", fill=(0, 0, 0))
    draw.text((50, 140), "Party B (Contractor): Tech Partner LLC", fill=(0, 0, 0))
    draw.text((50, 200), "1. Scope of Work: IT consulting services.", fill=(0, 0, 0))
    draw.text((50, 650), "Customer Signature: __________________", fill=(0, 0, 0))
    draw.text((50, 700), "Contractor Signature: ________________", fill=(0, 0, 0))
    draw.text((400, 650), "M.P. (Seal)", fill=(0, 0, 0))

    path = tmp_path / "blank_contract.png"
    img.save(path)
    return path


def _create_signed_and_stamped_contract(tmp_path: Path) -> Path:
    """Creates a contract with blue cursive signatures and a blue circular seal."""
    img = Image.new("RGB", (600, 800), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.text((50, 50), "ACCEPTANCE ACT # 44", fill=(0, 0, 0))
    draw.text((50, 100), "Customer: Global Corp LLC", fill=(0, 0, 0))
    draw.text((50, 140), "Contractor: Tech Partner LLC", fill=(0, 0, 0))
    draw.text((50, 650), "Customer Signature:", fill=(0, 0, 0))
    draw.text((50, 720), "Contractor Signature:", fill=(0, 0, 0))

    # Blue circular stamp around (420, 680)
    # Circle diameter ~ 80px
    stamp_bbox = [380, 640, 470, 730]
    draw.ellipse(stamp_bbox, outline=(20, 40, 200), width=4)
    draw.ellipse([395, 655, 455, 715], outline=(20, 40, 200), width=2)
    # Stamp text
    draw.text((405, 680), "ООО ТЕХНО", fill=(20, 40, 200))

    # Blue cursive signature strokes around (220, 650)
    sig_points = [
        (200, 660),
        (210, 640),
        (225, 670),
        (235, 645),
        (250, 665),
        (270, 635),
        (290, 660),
        (320, 655),
    ]
    draw.line(sig_points, fill=(10, 50, 220), width=3)
    draw.arc([230, 630, 280, 670], 0, 180, fill=(10, 50, 220), width=2)

    path = tmp_path / "executed_contract.png"
    img.save(path)
    return path


def _stamp_invoice(tmp_path: Path, stamp_text: str | None, name: str) -> Path:
    """An invoice with a red rectangular stamp, optionally carrying a status word."""
    img = Image.new("RGB", (600, 800), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.text((50, 50), "INVOICE # 9988", fill=(0, 0, 0))
    draw.text((50, 100), "Total: 5,000.00 EUR", fill=(0, 0, 0))

    draw.rectangle([250, 200, 450, 280], outline=(220, 20, 20), width=5)
    if stamp_text:
        draw.text((275, 222), stamp_text, fill=(220, 20, 20), font=ImageFont.load_default(size=30))

    path = tmp_path / name
    img.save(path)
    return path


def _create_paid_stamp_invoice(tmp_path: Path) -> Path:
    return _stamp_invoice(tmp_path, "PAID", "paid_invoice.png")


def _create_handwritten_correction_document(tmp_path: Path) -> Path:
    """Creates a document with handwritten correction marker."""
    img = Image.new("RGB", (600, 800), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.text((50, 50), "WAYBILL # 102", fill=(0, 0, 0))
    draw.text((50, 150), "Declared quantity: 100 pcs", fill=(0, 0, 0))
    # Strike through
    draw.line([(180, 155), (280, 155)], fill=(0, 0, 0), width=2)
    # Handwritten note
    draw.text((300, 150), "Correction: 85 pcs", fill=(20, 30, 180))
    draw.text((300, 170), "Corrected", fill=(20, 30, 180))

    path = tmp_path / "altered_waybill.png"
    img.save(path)
    return path


def test_blank_template_detection(tmp_path: Path):
    doc_path = _create_blank_contract_template(tmp_path)
    report = analyze_document_forensics(doc_path)

    assert report.is_empty_template is True
    assert report.has_signatures is False
    assert report.has_stamps is False
    assert report.is_executed is False
    assert "UNEXECUTED_TEMPLATE" in report.risk_flags


def test_signed_and_stamped_contract(tmp_path: Path):
    doc_path = _create_signed_and_stamped_contract(tmp_path)
    report = analyze_document_forensics(doc_path)

    assert report.is_empty_template is False
    assert report.is_executed is True
    assert report.has_stamps is True
    assert len(report.stamps) >= 1
    assert report.has_signatures is True
    assert len(report.signatures) >= 1

    stamp = report.stamps[0]
    assert stamp.color in {"blue", "violet"}
    assert stamp.shape in {"circular", "oval"}


def test_payment_stamp_detection(tmp_path: Path):
    doc_path = _create_paid_stamp_invoice(tmp_path)
    report = analyze_document_forensics(doc_path)

    assert report.has_stamps is True
    assert any(s.color == "red" for s in report.stamps)
    assert "PAYMENT_STAMP_PRESENT" in report.risk_flags


def test_handwritten_correction_detection(tmp_path: Path):
    doc_path = _create_handwritten_correction_document(tmp_path)
    report = analyze_document_forensics(doc_path)

    assert report.alterations_detected is True
    assert "HANDWRITTEN_ALTERATION" in report.risk_flags
    assert any(a.annotation_type == "price_correction" for a in report.annotations)


def test_page_forensics_direct():
    # Direct test on PIL Image
    img = Image.new("RGB", (300, 300), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Draw blue circular stamp
    draw.ellipse([50, 50, 150, 150], outline=(10, 40, 220), width=4)

    stamps, signatures, annotations = analyze_page_forensics(
        img, page_num=1, page_ocr_text="ПАО БАНК"
    )
    assert len(stamps) >= 1
    assert stamps[0].color == "blue"


def test_validate_with_forensic_report(tmp_path: Path):
    from datetime import date
    from docket.catalog import AcceptanceAct, AcceptanceActItem
    from docket.validate import validate

    blank_path = _create_blank_contract_template(tmp_path)
    report = analyze_document_forensics(blank_path)

    act = AcceptanceAct(
        act_number="ACT-001",
        act_date=date(2026, 9, 1),
        customer_name="Client LLC",
        contractor_name="Vendor LLC",
        items=[
            AcceptanceActItem(
                description="Services", quantity=1.0, unit_price=100.0, total=100.0
            )
        ],
        subtotal=100.0,
        total_amount=100.0,
    )

    issues = validate(act, forensic_report=report)
    error_messages = [i.message for i in issues if i.severity == "error"]
    assert any("blank template" in m.lower() for m in error_messages)


def test_red_stamp_without_status_word_is_not_a_payment_stamp(tmp_path: Path):
    report = analyze_document_forensics(_stamp_invoice(tmp_path, None, "plain.png"))
    assert report.has_stamps is True
    assert "PAYMENT_STAMP_PRESENT" not in report.risk_flags


def test_status_word_in_eu_language(tmp_path: Path):
    report = analyze_document_forensics(_stamp_invoice(tmp_path, "BEZAHLT", "bezahlt.png"))
    assert "PAYMENT_STAMP_PRESENT" in report.risk_flags
    void = analyze_document_forensics(_stamp_invoice(tmp_path, "STORNIERT", "storniert.png"))
    assert "VOID_STAMP_PRESENT" in void.risk_flags


def test_printed_paid_text_is_not_a_stamp(tmp_path: Path):
    img = Image.new("RGB", (600, 800), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((50, 50), "RECEIPT 00123", fill=(0, 0, 0), font=FONT)
    draw.text((50, 100), "Amount paid: 12.00 EUR", fill=(0, 0, 0), font=FONT)
    path = tmp_path / "receipt.png"
    img.save(path)

    report = analyze_document_forensics(path)
    assert "PAYMENT_STAMP_PRESENT" not in report.risk_flags
    assert report.has_stamps is False


def test_black_ink_signature_is_detected(tmp_path: Path):
    report = analyze_document_forensics(_black_signed_contract(tmp_path))
    assert report.has_signatures is True
    assert report.is_empty_template is False
    assert all(0 < s.confidence < 0.9 for s in report.signatures)


def test_black_seal_is_not_claimed_as_a_stamp(tmp_path: Path):
    # Black rings look like logos or table graphics to a pixel heuristic, so
    # the detector deliberately never reports black stamps.
    report = analyze_document_forensics(_black_signed_contract(tmp_path, with_seal=True))
    assert not any(s.color == "black" for s in report.stamps)
    assert report.has_signatures is True


def test_confidence_reflects_geometry():
    img = Image.new("RGB", (600, 800), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse([60, 500, 180, 620], outline=(10, 40, 220), width=4)  # round seal
    draw.rectangle([300, 500, 560, 540], outline=(220, 20, 20), width=4)  # flat red box
    stamps, _, _ = analyze_page_forensics(img)
    by_color = {s.color: s.confidence for s in stamps}
    assert by_color["blue"] > by_color["red"]
    assert len({s.confidence for s in stamps}) == len(stamps)
