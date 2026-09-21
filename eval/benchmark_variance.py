"""Extraction stability: the same document processed N times.

Temperature is 0, but a hosted LLM is still not perfectly deterministic
(batching, hardware rounding), and the interesting variance is not "did the
model return the identical JSON" but "did it read the coupon the same way".
The motivating case is `receipt_taxed`: a coupon line that must land in
`discount_amount` (not as a line item, not lost) so that
subtotal - discount + tax == total holds.

    python eval/benchmark_variance.py                      # receipt_taxed, 10 runs
    python eval/benchmark_variance.py --runs 5 --document path/to/doc.pdf

Writes eval/results/llm_variance.json and prints one line per field.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docket import __version__, process_document  # noqa: E402

DEFAULT_DOCUMENT = ROOT / "eval" / "golden_dataset" / "receipt_taxed.txt"
DEFAULT_OUT = ROOT / "eval" / "results" / "llm_variance.json"


def _canonical(value) -> str:
    """A JSON string that is stable across key order and float formatting."""
    def norm(v):
        if isinstance(v, float):
            return round(v, 2)
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items())}
        return v

    return json.dumps(norm(value), sort_keys=True, ensure_ascii=False)


def summarize_variance(runs: list[dict]) -> dict:
    """Per-field agreement across runs. A field agrees when every run that
    produced it produced the same value; fields missing in some runs count
    as a distinct value of `null` (they disagree)."""
    all_keys: set[str] = set()
    for run in runs:
        all_keys.update((run["extracted"] or {}).keys())
    fields: dict[str, Counter] = {key: Counter() for key in all_keys}
    for run in runs:
        extracted = run["extracted"] or {}
        for key in all_keys:
            fields[key][_canonical(extracted.get(key))] += 1

    per_field = {
        key: {"values": [[json.loads(v) if v != "null" else None, c] for v, c in counted.items()]}
        for key, counted in fields.items()
    }
    agreeing = sum(1 for counted in fields.values() if len(counted) == 1)
    return {
        "runs": len(runs),
        "statuses": dict(Counter(r["status"] for r in runs)),
        "needs_review": sum(bool(r["needs_review"]) for r in runs),
        "fields_total": len(fields),
        "fields_agreeing": agreeing,
        "agreement": round(agreeing / len(fields), 4) if fields else None,
        "seconds_mean": round(statistics.mean(r["seconds"] for r in runs), 2) if runs else None,
        "per_field": per_field,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    runs = []
    for i in range(args.runs):
        started = time.perf_counter()
        result = process_document(args.document)
        seconds = time.perf_counter() - started
        runs.append({
            "status": result.status.value,
            "schema_id": result.schema_id,
            "document_type": result.document_type,
            "needs_review": result.needs_review,
            "review_reasons": result.review_reasons,
            "extracted": result.extracted,
            "seconds": round(seconds, 2),
        })
        print(f"  run {i + 1}/{args.runs}: {result.status.value}"
              f"{' (review)' if result.needs_review else ''} {seconds:.1f}s", flush=True)

    report = {"document": args.document.name, "docket": __version__, **summarize_variance(runs), "runs_detail": runs}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = {k: v for k, v in report.items() if k not in ("runs_detail", "per_field")}
    print(json.dumps(summary, indent=2))
    disagreements = {k: v for k, v in report["per_field"].items() if len(v["values"]) > 1}
    print("\ndisagreeing fields:" if disagreements else "\nevery field identical across runs")
    for key, field in disagreements.items():
        print(f"  {key}:")
        for value, count in field["values"]:
            print(f"    {count}x {json.dumps(value, ensure_ascii=False)}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()