import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from metrics import (  # noqa: E402
    field_accuracy,
    field_precision_recall_f1,
    error_breakdown,
    review_safety_metrics,
    validation_detection_metrics,
)


def test_all_fields_match():
    correct, total, mismatches = field_accuracy(
        {"a": 1, "b": "Hello"}, {"a": 1, "b": "hello"}
    )
    assert (correct, total, mismatches) == (2, 2, [])


def test_float_tolerance():
    correct, total, _ = field_accuracy({"total": 10.004}, {"total": 10.0})
    assert (correct, total) == (1, 1)


def test_mismatch_is_reported():
    correct, total, mismatches = field_accuracy({"a": 1}, {"a": 2})
    assert (correct, total, mismatches) == (0, 1, ["a"])


def test_none_extracted_fails_everything():
    correct, total, mismatches = field_accuracy(None, {"a": 1, "b": 2})
    assert (correct, total, mismatches) == (0, 2, ["a", "b"])


def test_metadata_keys_are_not_graded():
    correct, total, _ = field_accuracy(
        {"a": 1}, {"a": 1, "_expect_validation_error_field": "x"}
    )
    assert (correct, total) == (1, 1)


def test_prf_all_correct_is_perfect():
    rows = [({"a": 1, "b": "x"}, {"a": 1, "b": "x"})]
    result = field_precision_recall_f1(rows)
    assert result["a"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0, "support": 1}
    assert result["_micro"]["f1"] == 1.0


def test_prf_wrong_value_hits_precision_and_recall():
    rows = [({"a": 2}, {"a": 1})]
    result = field_precision_recall_f1(rows)
    assert result["a"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 1}


def test_prf_missing_value_only_hits_recall():
    rows = [(None, {"a": 1})]
    result = field_precision_recall_f1(rows)
    # no FP was produced (nothing was output), so precision is undefined -> 0.0,
    # but recall is meaningfully 0 (the one expected value was never found)
    assert result["a"]["recall"] == 0.0
    assert result["a"]["support"] == 1


def test_prf_aggregates_across_documents():
    rows = [({"a": 1}, {"a": 1}), ({"a": 2}, {"a": 1})]
    result = field_precision_recall_f1(rows)
    assert result["a"]["support"] == 2
    assert result["a"]["precision"] == 0.5


def test_review_safety_counts_incorrect_documents_that_escape_review():
    rows = [
        {"classification_ok": True, "mismatches": [], "needs_review": False},
        {"classification_ok": False, "mismatches": [], "needs_review": False},
        {"classification_ok": True, "mismatches": ["total"], "needs_review": True},
    ]
    assert review_safety_metrics(rows) == {
        "incorrect_documents": 2,
        "incorrect_without_review": 1,
        "unsafe_pass_rate": 0.5,
    }


def test_validation_metrics_penalize_false_alarms():
    rows = [
        {
            "validation_expected": True,
            "validation_expected_detected": True,
            "validation_alerted": True,
        },
        {
            "validation_expected": False,
            "validation_expected_detected": False,
            "validation_alerted": True,
        },
    ]
    metrics = validation_detection_metrics(rows)
    assert metrics["precision"] == 0.5
    assert metrics["recall"] == 1.0


def test_error_breakdown_separates_failure_modes():
    rows = [
        {"classification_ok": False, "mismatches": [], "needs_review": False},
        {"classification_ok": True, "mismatches": ["total"], "needs_review": True},
    ]
    assert error_breakdown(rows) == {
        "pipeline_errors": 0,
        "classification_errors": 1,
        "field_extraction_errors": 1,
        "unsafe_passes": 1,
    }
