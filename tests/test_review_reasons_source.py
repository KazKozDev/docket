"""A key field read from words OCR was unsure of is not a success."""

from docket.result import SourceLocation
from docket.review_reasons import reasons_for
from tests.factories import make_result


def _with_total_confidence(confidence):
    return make_result(
        schema_id="invoice",
        field_sources={"total_amount": SourceLocation(page=1, quote="Total 18.00", confidence=confidence)},
    )


def test_low_ocr_confidence_on_a_key_field_needs_review():
    reasons = reasons_for(_with_total_confidence(0.41))
    assert any(r.startswith("total_amount was read from words OCR recognised with low confidence") for r in reasons)


def test_confident_or_text_layer_sources_pass():
    assert not any("low confidence" in r for r in reasons_for(_with_total_confidence(0.93)))
    assert not any("low confidence" in r for r in reasons_for(_with_total_confidence(None)))


def test_fields_outside_the_required_citations_are_not_gated():
    result = make_result(
        schema_id="invoice",
        field_sources={"notes": SourceLocation(page=1, quote="thanks", confidence=0.1)},
    )
    assert not any("low confidence" in r for r in reasons_for(result))
