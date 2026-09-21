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
        if got.strip().lower() == expected.strip().lower():
            return True
        # Identifiers printed in groups ("CH93 0076 2011 ...") are the same value.
        return not any(c.isspace() for c in expected.strip()) and "".join(got.split()).lower() == expected.strip().lower()
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


# ---- OCR benchmark: raw text, tables, line items ----------------------------------------------


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def word_scores(recognized: list[str], truth_lines: list[str]) -> dict[str, float | int]:
    """Order-insensitive word precision/recall/F1 of an OCR reading against the
    printed lines. Engines order columns and table cells differently, so a
    bag of words is the fair comparison; a word counts only if spelled exactly
    (case-insensitively), punctuation included."""
    from collections import Counter

    got = Counter(_norm(w) for w in recognized if w.strip())
    want = Counter(_norm(w) for line in truth_lines for w in line.split())
    hit = sum((got & want).values())
    precision = hit / sum(got.values()) if got else 0.0
    recall = hit / sum(want.values()) if want else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "words": sum(want.values())}


def table_cell_matches(detected: list[list[list[str]]], expected: list[list[str]]) -> int:
    """Expected cells found at the same row and column of the best-matching
    detected table (allowing the detected grid to start up to two rows
    earlier or later, e.g. a title row taken in or a header row missed)."""
    want = [[_norm(c) for c in row] for row in expected]
    best = 0
    for grid in detected:
        have = [[_norm(c) for c in row] for row in grid]
        for offset in range(-2, 3):
            hits = 0
            for r, row in enumerate(want):
                rr = r + offset
                if not 0 <= rr < len(have):
                    continue
                hits += sum(1 for c, cell in enumerate(row) if c < len(have[rr]) and cell and have[rr][c] == cell)
            best = max(best, hits)
    return best


def expected_cells(expected: list[list[str]]) -> int:
    return sum(1 for row in expected for cell in row if cell.strip())


def line_item_scores(extracted: list[dict], expected: list[dict]) -> dict[str, int]:
    """Greedy one-to-one matching of extracted to expected line items. An item
    is correct when its description matches (similarity >= 0.8 after
    normalization) and every numeric field the golden item lists is equal
    within 0.01. Items use the canonical names description / quantity /
    total (see SchemaSpec.line_items)."""
    from difflib import SequenceMatcher

    unused = list(range(len(extracted)))
    correct = 0
    for want in expected:
        for i in unused:
            got = extracted[i]
            ratio = SequenceMatcher(None, _norm(got.get("description") or ""), _norm(want["description"])).ratio()
            if ratio < 0.8:
                continue
            numbers_ok = True
            for key, value in want.items():
                if key == "description":
                    continue
                try:
                    numbers_ok &= abs(float(got.get(key)) - float(value)) <= 0.01
                except (TypeError, ValueError):
                    numbers_ok = False
            if numbers_ok:
                correct += 1
                unused.remove(i)
                break
    return {"correct": correct, "extracted": len(extracted), "expected": len(expected)}


def prf(correct: int, produced: int, expected: int) -> dict[str, float]:
    precision = correct / produced if produced else 0.0
    recall = correct / expected if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


# ---- Citation coverage (Stage 1 provenance benchmark) ------------------------------------------


def citation_coverage(result, expected: dict) -> dict:
    """How much of the golden document's material is backed by a provenance
    record: the share of graded fields — and of line-item fields, keyed by
    their schema paths (`items[0].price`, `transactions[0].amount`, ...) —
    that carry a citation in `result.field_sources`, and of those how many
    resolved to page geometry.

    Paths come from the golden file's dotted keys and `_line_items`, mapped
    through the registered schema's line-item spec, so the metric grades
    exactly what the golden set grades."""
    sources = getattr(result, "field_sources", None) or {}
    fields = [k for k in expected if not k.startswith("_") and k != "doc_type"]

    item_fields: list[str] = []
    spec_items = None
    try:
        from docket.catalog import get_schema
        spec = get_schema(result.schema_id) if result.schema_id else None
        spec_items = spec.line_items if spec is not None else None
    except Exception:
        spec_items = None
    if spec_items is not None:
        item_fields = [
            f"{spec_items.path}[{i}].{spec_items.columns.get(column, column)}"
            for i, item in enumerate(expected.get("_line_items") or [])
            for column in item
        ]
    else:  # no registered spec (custom schema): fall back to the canonical name
        item_fields = [
            f"line_items[{i}].{column}"
            for i, item in enumerate(expected.get("_line_items") or [])
            for column in item
        ]

    def covered(paths: list[str]) -> tuple[int, int, int]:
        cited = [p for p in paths if p in sources]
        located = [p for p in cited if getattr(sources[p], "bbox", None) is not None]
        return len(cited), len(located), len(paths)

    field_cited, field_located, field_total = covered(fields)
    item_cited, item_located, item_total = covered(item_fields)
    return {
        "fields": {"cited": field_cited, "located": field_located, "total": field_total},
        "line_items": {"cited": item_cited, "located": item_located, "total": item_total},
    }


def citation_coverage_summary(rows: list[dict]) -> dict:
    """Aggregate the per-document coverage counts: the share of graded fields
    and line-item fields cited and located, over every document that graded
    anything. The line-item share is the Stage 1 acceptance number."""
    def share(kind: str, key: str) -> float | None:
        rows_with_counts = [r["citations"] for r in rows if r.get("citations")]
        total = sum(c[kind]["total"] for c in rows_with_counts)
        if not total:
            return None
        return round(sum(c[kind][key] for c in rows_with_counts) / total, 4)

    return {
        "field_citation_coverage": share("fields", "cited"),
        "field_location_coverage": share("fields", "located"),
        "line_item_citation_coverage": share("line_items", "cited"),
        "line_item_location_coverage": share("line_items", "located"),
    }
