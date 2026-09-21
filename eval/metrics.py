"""Field-level scoring: compares an extracted dict against a golden dict.

Kept separate from run_eval.py so it can be unit-tested without touching
the LLM — see tests/test_metrics.py.
"""
from __future__ import annotations


def value_at(data: dict | None, path: str):
    """Value at a dotted path with optional list indices; None if absent."""
    current: object = data
    for part in path.split("."):
        name, _, index = part.partition("[")
        if not isinstance(current, dict):
            return None
        current = current.get(name)
        if index:
            try:
                current = current[int(index.rstrip("]"))]  # type: ignore[index]
            except (IndexError, TypeError, ValueError):
                return None
    return current


def field_accuracy(extracted: dict | None, expected: dict) -> tuple[int, int, list[str]]:
    """Returns (n_correct, n_total, mismatched_field_names).

    Only checks keys present in `expected` (minus metadata keys starting
    with `_`, and `doc_type`, which grades classification) — golden files
    don't have to enumerate every schema field, just the ones worth grading.
    Keys may be field paths into nested data: "seller.name",
    "seller.tax_ids[0].value".
    """
    graded_keys = [k for k in expected if not k.startswith("_") and k != "doc_type"]
    if extracted is None:
        return 0, len(graded_keys), graded_keys

    mismatches = []
    correct = 0
    for key in graded_keys:
        exp_val = expected[key]
        got_val = value_at(extracted, key)
        if _values_match(got_val, exp_val):
            correct += 1
        else:
            mismatches.append(key)
    return correct, len(graded_keys), mismatches


def _values_match(got, expected) -> bool:
    if isinstance(expected, float) or isinstance(got, float):
        try:
            return abs(float(got) - float(expected)) <= 0.01
        except (TypeError, ValueError):
            return False
    if isinstance(expected, str) and isinstance(got, str):
        return got.strip().lower() == expected.strip().lower()
    return got == expected


def field_precision_recall_f1(rows: list[tuple[dict | None, dict]]) -> dict[str, dict[str, float]]:
    """Per-field precision/recall/F1 aggregated across a batch of
    (extracted, expected) pairs, plus a "_micro" row summed over all fields.

    Convention (standard for slot/field extraction, not just classification):
    a field is graded once per document where `expected` names it.
      - correct value produced            -> TP
      - wrong value produced (not missing) -> FP and FN both (a wrong
        answer is a false positive on its own value AND a false negative on
        the correct one, since the correct value was never delivered)
      - no value produced (None/missing)   -> FN only
    There's no TN/FP-from-nothing case here because golden files only list
    fields worth grading — a field the schema has but the document doesn't
    mention is never asked about.
    """
    counts: dict[str, dict[str, int]] = {}

    for extracted, expected in rows:
        for key, exp_val in expected.items():
            if key.startswith("_") or key == "doc_type":
                continue
            c = counts.setdefault(key, {"tp": 0, "fp": 0, "fn": 0})
            got_val = value_at(extracted, key) if extracted is not None else None
            if got_val is None:
                c["fn"] += 1
            elif _values_match(got_val, exp_val):
                c["tp"] += 1
            else:
                c["fp"] += 1
                c["fn"] += 1

    def _prf(c: dict[str, int]) -> dict[str, float]:
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3), "support": tp + fn}

    result = {field: _prf(c) for field, c in counts.items()}
    micro = {"tp": sum(c["tp"] for c in counts.values()), "fp": sum(c["fp"] for c in counts.values()), "fn": sum(c["fn"] for c in counts.values())}
    result["_micro"] = _prf(micro)
    return result


def review_safety_metrics(rows: list[dict]) -> dict[str, float | int]:
    """Measure incorrect documents that escaped mandatory human review."""
    incorrect = [
        row
        for row in rows
        if not row.get("classification_ok", False) or bool(row.get("mismatches")) or row.get("error")
    ]
    escaped = [row for row in incorrect if not row.get("needs_review", False)]
    return {
        "incorrect_documents": len(incorrect),
        "incorrect_without_review": len(escaped),
        "unsafe_pass_rate": round(len(escaped) / len(incorrect), 3) if incorrect else 0.0,
    }


def validation_detection_metrics(rows: list[dict]) -> dict[str, float | int]:
    """Document-level precision/recall for validation alerts, including false alarms."""
    tp = sum(
        bool(row.get("validation_expected"))
        and bool(row.get("validation_expected_detected"))
        for row in rows
    )
    fn = sum(
        bool(row.get("validation_expected"))
        and not bool(row.get("validation_expected_detected"))
        for row in rows
    )
    fp = sum(
        bool(row.get("validation_alerted"))
        and (
            not bool(row.get("validation_expected"))
            or not bool(row.get("validation_expected_detected"))
        )
        for row in rows
    )
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def error_breakdown(rows: list[dict]) -> dict[str, int]:
    """Small actionable taxonomy for every failed evaluation row."""
    return {
        "pipeline_errors": sum(bool(row.get("error")) for row in rows),
        "classification_errors": sum(not row.get("classification_ok", False) for row in rows),
        "field_extraction_errors": sum(bool(row.get("mismatches")) for row in rows),
        "unsafe_passes": sum(
            (not row.get("classification_ok", False) or bool(row.get("mismatches")) or bool(row.get("error")))
            and not row.get("needs_review", False)
            for row in rows
        ),
    }
