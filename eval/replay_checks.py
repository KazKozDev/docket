"""Re-grade a saved pipeline run against the current checks and labels. No LLM.

    python eval/replay_checks.py eval/results/zugferd_pdf.json eval/zugferd_samples/pdf

A pipeline run (benchmark_ocr.py --save-results) keeps every DocumentResult
with its page text. This script takes those extractions as they are and
recomputes two things the model has no part in:

- the deterministic checks, by passing each extraction and its pages to
  `docket.verify()`; review reasons that are not validation errors
  (classification or OCR confidence) are kept from the run;
- the grading, against the expected.json files as they are now.

The run's own file is left untouched; the replay is written next to it as
<name>.replayed.json, so both can be compared.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from metrics import field_accuracy

from docket import DocumentResult, verify


def replay(run_path: Path, dataset: Path) -> dict:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    results_dir = run_path.parent / (run_path.stem + "_results")
    rows = []
    for config, block in run["pipeline"].items():
        for row in block["documents"]:
            result = DocumentResult.model_validate_json(
                (results_dir / f"{row['document']}.result.json").read_text(encoding="utf-8"))
            expected = json.loads((dataset / (Path(row["document"]).stem + ".expected.json")).read_text(encoding="utf-8"))
            classified = result.document_type == expected.get("doc_type")
            reasons = [r for r in row["review_reasons"] if not r.startswith("validation error")]
            if result.document is not None and result.layout is not None:
                doc = result.extracted | {"field_locations": {
                    k: {"page": v.page, "quote": v.quote} for k, v in result.field_sources.items()}}
                checked = verify(doc, [p.text for p in result.layout.pages], document_type=result.document_type)
                reasons += [r for r in checked.review_reasons if r.startswith("validation error")]
            correct, total, mismatches = field_accuracy(result.extracted if classified else None, expected)
            rows.append({
                "config": config, "document": row["document"], "status": result.status.value,
                "classified": classified, "fields": {"correct": correct, "total": total, "mismatches": mismatches},
                "success": result.status.value != "failed" and classified and correct == total,
                "needs_review": result.status.value == "failed" or bool(reasons), "review_reasons": reasons,
            })
    silent = [r for r in rows if not r["needs_review"]]
    summary = {
        "documents": len(rows),
        "fully_right": sum(r["success"] for r in rows),
        "field_accuracy": round(sum(r["fields"]["correct"] for r in rows) / max(1, sum(r["fields"]["total"] for r in rows)), 4),
        "needs_review": sum(r["needs_review"] for r in rows),
        "silent_successes": len(silent),
        "false_successes": sum(not r["success"] for r in silent),
    }
    return {"replayed_from": run_path.name, "summary": summary, "documents": rows}


def main() -> None:
    run_path, dataset = Path(sys.argv[1]), Path(sys.argv[2])
    report = replay(run_path, dataset)
    out = run_path.with_name(run_path.stem + ".replayed.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
