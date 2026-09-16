"""Runs the full docket pipeline over a directory of documents and reports
classification accuracy, field-level precision/recall/F1, the share of
documents that would be queued for human review, and latency/cost per
document.

    python eval/run_eval.py                    # eval/golden_dataset (default)
    python eval/run_eval.py eval/real_samples   # any directory in the same layout
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from metrics import (  # noqa: E402
    field_accuracy,
    field_precision_recall_f1,
    error_breakdown,
    review_safety_metrics,
    validation_detection_metrics,
)

from docket import config  # noqa: E402
from docket.pipeline import process  # noqa: E402

DEFAULT_DIR = Path(__file__).parent / "golden_dataset"
RESULTS_DIR = Path(__file__).parent / "results"


def main() -> None:
    doc_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR

    RESULTS_DIR.mkdir(exist_ok=True)
    doc_paths = sorted(
        p for ext in ("*.txt", "*.pdf", "*.png", "*.jpg") for p in doc_dir.glob(ext)
    )
    if not doc_paths:
        print(f"No documents found in {doc_dir}")
        return

    rows = []
    field_pairs: list[tuple[dict | None, dict]] = []
    for doc_path in doc_paths:
        expected_path = doc_path.with_suffix(".expected.json")
        expected = json.loads(expected_path.read_text())

        start = time.perf_counter()
        try:
            result = process(doc_path)
        except Exception as exc:  # noqa: BLE001
            # One unreachable model or unreadable file shouldn't cost you the
            # other eighteen documents' worth of results.
            print(f"    ERROR on {doc_path.name}: {type(exc).__name__}: {exc}")
            rows.append(
                {
                    "doc": doc_path.name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "classification_ok": False,
                    "classified_as": "-",
                    "classification_method": "-",
                    "ocr_method": "-",
                    "field_accuracy": "0/0",
                    "mismatches": [],
                    "validation_caught_expected_error": False,
                    "validation_expected": bool(expected.get("_expect_validation_error_field")),
                    "validation_expected_detected": False,
                    "validation_alerted": False,
                    "extract_attempts": 0,
                    "needs_review": True,
                    "latency_s": round(time.perf_counter() - start, 2),
                    "llm_calls": 0,
                    "llm_estimated_tokens": 0,
                    "est_cloud_cost_usd": 0.0,
                }
            )
            field_pairs.append((None, expected))
            continue
        latency_s = time.perf_counter() - start

        correct, total, mismatches = field_accuracy(result.extracted, expected)
        classification_ok = result.classification.doc_type.value == expected.get("doc_type")

        # A document whose correct answer is "unknown" has no fields to
        # extract — grading one anyway would count a correct abstention as a
        # field-level miss and quietly drag F1 down with it.
        correctly_abstained = expected.get("doc_type") == "unknown" and classification_ok
        if correctly_abstained:
            correct, total, mismatches = 0, 0, []
        else:
            field_pairs.append((result.extracted, expected))
        expected_validation_field = expected.get("_expect_validation_error_field")
        validation_errors = [issue for issue in result.validation_issues if issue.severity == "error"]
        validation_detected = bool(validation_errors)
        caught_expected_error = expected_validation_field is not None and any(
            issue.field == expected_validation_field for issue in result.validation_issues
        )
        validation_correct = caught_expected_error if expected_validation_field else not validation_detected
        est_cost_usd = (
            result.llm_estimated_tokens / 1_000_000
        ) * config.CLOUD_EQUIVALENT_USD_PER_1M_TOKENS

        rows.append(
            {
                "doc": doc_path.name,
                "classification_ok": classification_ok,
                "classified_as": result.classification.doc_type.value,
                "classification_method": result.classification.method,
                "ocr_method": result.ocr_method + ("*" if result.escalated_to_vlm else ""),
                "field_accuracy": "n/a" if correctly_abstained else f"{correct}/{total}",
                "mismatches": mismatches,
                "validation_caught_expected_error": validation_correct,
                "validation_expected": expected_validation_field is not None,
                "validation_expected_detected": caught_expected_error,
                "validation_alerted": validation_detected,
                "extract_attempts": result.extract_attempts,
                "needs_review": result.needs_review,
                "latency_s": round(latency_s, 2),
                "llm_calls": result.llm_calls,
                "llm_estimated_tokens": result.llm_estimated_tokens,
                "est_cloud_cost_usd": round(est_cost_usd, 6),
            }
        )

    prf = field_precision_recall_f1(field_pairs)
    safety = review_safety_metrics(rows)
    validation_metrics = validation_detection_metrics(rows)
    errors = error_breakdown(rows)
    _print_report(rows, prf, safety, validation_metrics, errors)
    (RESULTS_DIR / f"last_run_{doc_dir.name}.json").write_text(
        json.dumps(
            {
                "rows": rows,
                "field_precision_recall_f1": prf,
                "review_safety": safety,
                "validation_detection": validation_metrics,
                "error_breakdown": errors,
            },
            indent=2,
        )
    )


def _print_report(
    rows: list[dict],
    prf: dict[str, dict],
    safety: dict[str, float | int],
    validation_metrics: dict[str, float | int],
    errors: dict[str, int],
) -> None:
    print(f"{'doc':<28} {'class':<8} {'method':<6} {'ocr':<14} {'fields':<8} {'val_ok':<7} {'review':<7} {'latency':<8} {'retries'}")
    print("-" * 100)
    for r in rows:
        print(
            f"{r['doc']:<28} "
            f"{'OK' if r['classification_ok'] else 'FAIL':<8} "
            f"{r['classification_method']:<6} "
            f"{r['ocr_method']:<14} "
            f"{r['field_accuracy']:<8} "
            f"{'OK' if r['validation_caught_expected_error'] else 'FAIL':<7} "
            f"{'YES' if r['needs_review'] else '-':<7} "
            f"{r['latency_s']:>6.2f}s "
            f"{r['extract_attempts']}"
        )
        if r["mismatches"]:
            print(f"    mismatched fields: {r['mismatches']}")

    if any(r["ocr_method"].endswith("*") for r in rows):
        print("  (* = re-read by the vision model after validation failed on the OCR text)")

    n = len(rows)
    class_acc = sum(r["classification_ok"] for r in rows) / n
    val_acc = sum(r["validation_caught_expected_error"] for r in rows) / n
    review_rate = sum(r["needs_review"] for r in rows) / n
    total_latency = sum(r["latency_s"] for r in rows)
    total_tokens = sum(r["llm_estimated_tokens"] for r in rows)
    total_cost = sum(r["est_cloud_cost_usd"] for r in rows)

    print("-" * 100)
    print(f"classification accuracy: {class_acc:.0%}   validation-catch accuracy: {val_acc:.0%}   review rate: {review_rate:.0%}")
    print(
        "validation detection: "
        f"precision={validation_metrics['precision']:.2f} "
        f"recall={validation_metrics['recall']:.2f} f1={validation_metrics['f1']:.2f}   "
        f"unsafe pass: {safety['incorrect_without_review']}/{safety['incorrect_documents']} "
        f"({safety['unsafe_pass_rate']:.0%})"
    )
    print(
        "errors: "
        f"pipeline={errors['pipeline_errors']} "
        f"classification={errors['classification_errors']} "
        f"fields={errors['field_extraction_errors']} "
        f"unsafe_passes={errors['unsafe_passes']}"
    )
    local_models = not (config.TEXT_MODEL.endswith(":cloud") or config.VISION_MODEL.endswith(":cloud"))
    actual_cost = "$0.00 actual (local Ollama)" if local_models else "actual cloud cost unavailable from Ollama"
    print(
        f"latency: {total_latency:.1f}s total / {total_latency / n:.2f}s per doc   "
        f"tokens: ~{total_tokens} total   "
        f"cost: {actual_cost} — ~${total_cost:.4f} at a "
        f"${config.CLOUD_EQUIVALENT_USD_PER_1M_TOKENS:.2f}/1M-token hosted model"
    )

    micro = prf.get("_micro")
    if micro:
        print(f"field-level (micro, n={micro['support']}): precision={micro['precision']:.2f}  recall={micro['recall']:.2f}  f1={micro['f1']:.2f}")
    print("field-level precision/recall/f1 by field:")
    for field, m in sorted(prf.items()):
        if field == "_micro" or m["support"] == 0:
            continue
        print(f"    {field:<20} P={m['precision']:.2f}  R={m['recall']:.2f}  F1={m['f1']:.2f}  (n={m['support']})")


if __name__ == "__main__":
    main()
