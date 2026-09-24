"""One-shot analysis of the `extended_pr30` benchmark run: false-success rate,
review-rule effectiveness, and `DOCKET_MIN_SOURCE_CONFIDENCE` calibration.

    python eval/analyze_pr30.py

Reads only the saved checkpoint (and the previous `extended_stage2.json` for
comparison); never runs the pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
ROWS = [json.loads(line) for line in (RESULTS / "extended_pr30.jsonl").read_text().splitlines() if line.strip()]
THRESHOLDS = (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9)


def rate(rows: list[dict]) -> str:
    silent = [r for r in rows if r["status"] == "succeeded" and not r["needs_review"]]
    false = [r for r in silent if not r["success"]]
    return f"{len(false)}/{len(silent)} ({len(false) / len(silent):.1%})" if silent else "0/0"


def low_confidence_flagged(r: dict, threshold: float) -> bool:
    return any(v is not None and v < threshold for v in r["key_source_confidence"].values())


def has_other_reason(r: dict) -> bool:
    return any("with low confidence" not in x for x in r["review_reasons"]) or r["status"] == "failed"


def stats(rows: list[dict], threshold: float) -> tuple[str, str]:
    """False-success rate and review share if the threshold were `threshold`.

    Everything but the low-confidence rule is replayed from the saved
    review_reasons; the low-confidence rule is replayed from the saved
    key_source_confidence (a None confidence is no signal either way).
    """
    silent = [r for r in rows if r["status"] == "succeeded"
              and not low_confidence_flagged(r, threshold) and not has_other_reason(r)]
    false = [r for r in silent if not r["success"]]
    reviewed = sum(1 for r in rows if low_confidence_flagged(r, threshold) or has_other_reason(r))
    fs = f"{len(false)}/{len(silent)} ({len(false) / len(silent):.1%})" if silent else "0/0"
    return fs, f"{reviewed}/{len(rows)} ({reviewed / len(rows):.1%})"


print("=== false-success rate (silent successes with >= 1 wrong field) ===")
print(f"all docs:         {rate(ROWS)}")
print(f"excluding sroie:  {rate([r for r in ROWS if r['source'] != 'sroie'])}")
old_path = RESULTS / "extended_stage2.json"
if old_path.exists():
    old = json.loads(old_path.read_text())["pipeline"]["tesseract"]["documents"]
    old_nonsroie = [r for r in old if not r["document"].startswith("receipt_sroie")]
    print(f"previous run:     {rate(old)}  ({rate(old_nonsroie)} excluding sroie)")

print("\n=== review rules: reviews triggered, and was the document right? ===")
for label, needle in [("low confidence", "with low confidence"), ("doc-number not printed", "does not print it")]:
    hit = [r for r in ROWS if any(needle in x for x in r["review_reasons"])]
    right = [r for r in hit if r["success"]]
    print(f"{label:22} flagged {len(hit):3}  right (needless review) {len(right):3}  "
          f"wrong (caught) {len(hit) - len(right):3}")

print(f"\n=== DOCKET_MIN_SOURCE_CONFIDENCE calibration (all {len(ROWS)} docs) ===")
print(f"{'threshold':>9} {'false successes':>16} {'docs to review':>16}")
for t in THRESHOLDS:
    fs, rv = stats(ROWS, t)
    print(f"{t:>9} {fs:>16} {rv:>16}")

print("\n=== false successes at the previous default, 0.75 ===")
for r in [r for r in ROWS if r["status"] == "succeeded" and not r["needs_review"] and not r["success"]]:
    print(f"  {r['document']:40} {r['source']:8} {r['key_source_confidence']}")