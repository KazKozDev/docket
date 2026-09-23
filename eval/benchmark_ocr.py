"""Tesseract vs PaddleOCR on the scanned documents of the eval sets.

    python eval/benchmark_ocr.py                        # OCR-only and full pipeline, all configs
    python eval/benchmark_ocr.py --ocr-only             # no LLM calls: raw text and tables only
    python eval/benchmark_ocr.py --configs tesseract paddle-mobile --out results.json
    python eval/benchmark_ocr.py --configs tesseract --pipeline-only --checkpoint run.jsonl   # resumable

Configs: `tesseract`, `paddle-mobile`, `paddle-medium` (PaddleOCR PP-OCRv5
mobile / server recognition models). Each is run twice:

- **ocr-only**: `docket.ocr.acquire()` with that backend and no fallback,
  scored on documents with ground truth for the printed text (`_text`) and
  tables (`_tables`) — word precision/recall/F1 and table cell accuracy.
- **pipeline**: `process_document()` with that backend and the vision-model
  fallback (the default chain), classification included, scored on every
  labeled scan: document success (classified correctly and every graded
  field right), field accuracy, line-item precision/recall/F1, table cell
  accuracy, time per document (mean, median), share of pages and documents
  that fell back to the vision model, LLM calls, documents sent to review.

Only scans are used: images, and PDFs without a text layer (a text layer is
read without OCR, so it says nothing about the engines). Each backend reads
a small warm-up image before timing starts, so model loading is excluded
and reported separately. Results go to eval/results/ocr_benchmark.json with
the machine, library and model versions they were measured with. LLM
answers vary between runs; the JSON keeps every document's outcome so
differences can be traced.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import (  # noqa: E402
    citation_coverage,
    citation_coverage_summary,
    expected_cells,
    field_accuracy,
    line_item_scores,
    prf,
    table_cell_matches,
    value_at,
    word_scores,
)

from docket import __version__, catalog, config  # noqa: E402
from docket.ocr import AcquisitionOptions, OcrSettings, acquire  # noqa: E402
from docket.options import OcrOptions, ProcessOptions, ReviewOptions  # noqa: E402
from docket.pipeline import process_document  # noqa: E402

DATASETS = [ROOT / "eval" / "golden_dataset", ROOT / "eval" / "real_samples"]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
LANGUAGES = ["en", "de", "es", "fr"]
CONFIGS = {
    "tesseract": {"backend": "tesseract", "paddle_model": None},
    "paddle-mobile": {"backend": "paddle", "paddle_model": "mobile"},
    "paddle-medium": {"backend": "paddle", "paddle_model": "medium"},
}


def is_scan(path: Path) -> bool:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return True
    if path.suffix.lower() != ".pdf":
        return False
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return not any(page.chars for page in pdf.pages)


def documents(dirs: list[Path]) -> list[tuple[Path, dict]]:
    found = []
    for folder in dirs:
        for expected_path in sorted(folder.glob("*.expected.json")):
            stem = expected_path.name[: -len(".expected.json")]
            for candidate in sorted(folder.glob(f"{stem}.*")):
                if candidate != expected_path and is_scan(candidate):
                    found.append((candidate, json.loads(expected_path.read_text(encoding="utf-8"))))
    return found


def ocr_options(name: str) -> OcrOptions:
    spec = CONFIGS[name]
    return OcrOptions(backend=spec["backend"], languages=LANGUAGES, paddle_model=spec["paddle_model"])


def acquisition_options(name: str) -> AcquisitionOptions:
    spec = CONFIGS[name]
    settings = OcrSettings(languages=LANGUAGES, **({"paddle_model": spec["paddle_model"]} if spec["paddle_model"] else {}))
    return AcquisitionOptions(backend=spec["backend"], fallbacks=[], settings=settings)


def warm_up(name: str) -> float:
    """Load the engine on a tiny image so model loading doesn't count as reading time."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (400, 120), 255)
    ImageDraw.Draw(image).text((20, 40), "Warm up 123", fill=0)
    path = ROOT / ".cache" / "benchmark-warmup.png"
    path.parent.mkdir(exist_ok=True)
    image.save(path)
    started = time.perf_counter()
    acquire(path, acquisition_options(name))
    return round(time.perf_counter() - started, 2)


def page_grids(layout) -> dict[int, list[list[list[str]]]]:
    return {page.page_number: [table.grid() for table in page.tables] for page in layout.pages}


def table_score(layout, expected: dict) -> tuple[int, int]:
    grids = page_grids(layout) if layout is not None else {}
    matched = total = 0
    for table in expected.get("_tables", []):
        total += expected_cells(table["cells"])
        matched += table_cell_matches(grids.get(table["page"], []), table["cells"])
    return matched, total


# ---- ocr-only -----------------------------------------------------------------------------


def run_ocr_only(name: str, docs: list[tuple[Path, dict]]) -> dict:
    rows = []
    for path, expected in docs:
        if "_text" not in expected:
            continue
        started = time.perf_counter()
        try:
            acquisition = acquire(path, acquisition_options(name))
        except Exception as exc:  # noqa: BLE001 - one unreadable scan must not end the run
            rows.append({"document": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        seconds = time.perf_counter() - started
        words = [w.text for page in acquisition.layout.pages for w in page.words]
        matched, total = table_score(acquisition.layout, expected)
        rows.append({
            "document": path.name,
            "seconds": round(seconds, 2),
            "pages": len(acquisition.layout.pages),
            "confidence": [p.confidence for p in acquisition.report.pages],
            "words": word_scores(words, expected["_text"]),
            "table_cells": {"matched": matched, "expected": total},
        })
    ok = [r for r in rows if "error" not in r]
    cells_matched = sum(r["table_cells"]["matched"] for r in ok)
    cells_total = sum(r["table_cells"]["expected"] for r in ok)
    times = [r["seconds"] for r in ok]
    return {
        "summary": {
            "documents": len(rows),
            "errors": len(rows) - len(ok),
            "word_f1_mean": round(statistics.mean(r["words"]["f1"] for r in ok), 4) if ok else None,
            "word_recall_mean": round(statistics.mean(r["words"]["recall"] for r in ok), 4) if ok else None,
            "table_cell_accuracy": round(cells_matched / cells_total, 4) if cells_total else None,
            "table_cells_expected": cells_total,
            "seconds_mean": round(statistics.mean(times), 2) if times else None,
            "seconds_median": round(statistics.median(times), 2) if times else None,
        },
        "documents": rows,
    }


# ---- full pipeline ---------------------------------------------------------------------------


def canonical_items(extracted: dict | None, schema_id: str | None) -> list[dict]:
    if not extracted or not schema_id:
        return []
    try:
        spec = catalog.get_schema(schema_id)
    except catalog.SchemaError:
        return []
    mapping = spec.line_items
    if mapping is None:
        return []
    items = value_at(extracted, mapping.path) or []
    return [{name: item.get(source) for name, source in mapping.columns.items()} for item in items]


def _load_checkpoint(checkpoint: Path | None, name: str) -> dict[str, dict]:
    """Rows already finished for config `name`, by document, from a
    --checkpoint file (JSON Lines, one row per finished document)."""
    if checkpoint is None or not checkpoint.exists():
        return {}
    done = {}
    for line in checkpoint.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.pop("_config", None) == name:
                done[row["document"]] = row
    return done


def run_pipeline(name: str, docs: list[tuple[Path, dict]], checkpoint: Path | None = None) -> dict:
    options = ProcessOptions(
        ocr=ocr_options(name).model_copy(update={"fallbacks": ["vlm"]}),
        include_layout=True,
        review=ReviewOptions(enqueue=False),
    )
    done = _load_checkpoint(checkpoint, name)
    if done:
        print(f"  resuming: {len(done)} documents already in {checkpoint}", flush=True)
    rows = []
    for path, expected in docs:
        if path.name in done:
            rows.append(done[path.name])
            continue
        started = time.perf_counter()
        result = process_document(path, options)
        seconds = time.perf_counter() - started
        classified = result.document_type == expected.get("doc_type")
        correct, total, mismatches = field_accuracy(result.extracted if classified else None, expected)
        items = line_item_scores(canonical_items(result.extracted, result.schema_id) if classified else [],
                                 expected.get("_line_items", []))
        matched, cells = table_score(result.layout, expected)
        pages = result.ocr.pages if result.ocr else []
        vlm_pages = sum(1 for p in pages if p.backend == "vlm")
        rows.append({
            "document": path.name,
            "status": result.status.value,
            "error": result.error.code if result.error else None,
            "expected_type": expected.get("doc_type"),
            "classified_as": result.document_type,
            "classification_method": result.classification.method if result.classification else None,
            "fields": {"correct": correct, "total": total, "mismatches": mismatches,
                       "extracted": {k: value_at(result.extracted, k) for k in mismatches}},
            "line_items": items,
            "citations": citation_coverage(result, expected),
            "table_cells": {"matched": matched, "expected": cells},
            "success": result.status.value != "failed" and classified and correct == total,
            "needs_review": result.needs_review,
            "review_reasons": result.review_reasons,
            "seconds": round(seconds, 2),
            "acquire_seconds": result.metrics.stage_seconds.get("acquire"),
            "pages": len(pages),
            "vlm_pages": vlm_pages,
            "escalated_to_vlm": result.metrics.escalated_to_vlm,
            "llm_calls": result.metrics.llm_calls,
            "template_id": result.metrics.template_id,
        })
        if checkpoint is not None:
            with checkpoint.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"_config": name, **rows[-1]}, ensure_ascii=False) + "\n")
        print(f"  {name:14} {path.name:36} {'ok' if rows[-1]['success'] else 'FAIL':4} "
              f"fields {correct}/{total} items {items['correct']}/{items['expected']} "
              f"{seconds:6.1f}s vlm_pages={vlm_pages} llm={result.metrics.llm_calls}", flush=True)
    return {"summary": summarize(rows), "documents": rows}


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    times = [r["seconds"] for r in rows]
    acquire_times = [r["acquire_seconds"] for r in rows if r["acquire_seconds"] is not None]
    fields_correct = sum(r["fields"]["correct"] for r in rows)
    fields_total = sum(r["fields"]["total"] for r in rows)
    items = [r["line_items"] for r in rows]
    citation = citation_coverage_summary(rows)
    graded = [i for i in items if i["expected"]]  # precision needs golden line items; unmarked docs are not graded
    cells_matched = sum(r["table_cells"]["matched"] for r in rows)
    cells_total = sum(r["table_cells"]["expected"] for r in rows)
    pages = sum(r["pages"] for r in rows)
    # A false success is the worst outcome: the pipeline answered "succeeded,
    # no review needed" and the graded truth disagrees — a wrong number nobody
    # will look at. Docs that went to review or failed made no clean claim,
    # so the rate is taken over the silent successes only.
    silent = [r for r in rows if r["status"] == "succeeded" and not r["needs_review"]]
    false_successes = [r for r in silent if not r["success"]]
    templated = [r for r in rows if r.get("template_id")]
    return {
        "documents": n,
        "document_success_rate": round(sum(r["success"] for r in rows) / n, 4) if n else None,
        "field_accuracy": round(fields_correct / fields_total, 4) if fields_total else None,
        "fields_graded": fields_total,
        "line_items": {**prf(sum(i["correct"] for i in items), sum(i["extracted"] for i in graded),
                             sum(i["expected"] for i in items)),
                       "expected": sum(i["expected"] for i in items),
                       "documents_graded": len(graded)},
        "table_cell_accuracy": round(cells_matched / cells_total, 4) if cells_total else None,
        "seconds_mean": round(statistics.mean(times), 2) if times else None,
        "seconds_median": round(statistics.median(times), 2) if times else None,
        "acquire_seconds_mean": round(statistics.mean(acquire_times), 2) if acquire_times else None,
        "vlm_page_share": round(sum(r["vlm_pages"] for r in rows) / pages, 4) if pages else None,
        "vlm_document_share": round(sum(1 for r in rows if r["vlm_pages"] or r["escalated_to_vlm"]) / n, 4) if n else None,
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "llm_calls_mean": round(sum(r["llm_calls"] for r in rows) / n, 2) if n else None,
        "templates": {
            "documents": len(templated),
            "hit_rate": round(len(templated) / n, 4) if n else None,
            "seconds_mean": round(statistics.mean(r["seconds"] for r in templated), 2) if templated else None,
            # A successful template bypasses at least the first structured-extraction call.
            "llm_extraction_calls_avoided_minimum": len(templated),
            "by_id": dict(sorted(__import__("collections").Counter(r["template_id"] for r in templated).items())),
        },
        "citations": citation,
        "silent_successes": len(silent),
        "false_successes": len(false_successes),
        "false_success_rate": round(len(false_successes) / len(silent), 4) if silent else None,
        "needs_review": sum(r["needs_review"] for r in rows),
        "failed": sum(r["status"] == "failed" for r in rows),
    }


# ---- report ---------------------------------------------------------------------------------------


def environment(configs: list[str]) -> dict:
    def version(module: str) -> str | None:
        try:
            from importlib.metadata import version as v

            return v(module)
        except Exception:  # noqa: BLE001
            return None

    try:
        tesseract = subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.split("\n")[0]
    except OSError:
        tesseract = None
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                cwd=ROOT).stdout.strip()
    except OSError:
        commit = None
    return {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "docket": __version__,
        "git_commit": commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": __import__("os").cpu_count(),
        "tesseract": tesseract,
        "paddleocr": version("paddleocr"),
        "paddlepaddle": version("paddlepaddle"),
        "llm_provider": config.LLM_PROVIDER,
        "text_model": config.TEXT_MODEL,
        "vision_model": config.VISION_MODEL,
        "ocr_languages": LANGUAGES,
        "ocr_dpi": config.OCR_DPI,
        "layout_markers": config.LAYOUT_MARKERS,
        "configs": configs,
    }


def print_summary(report: dict) -> None:
    print()
    if report.get("ocr_only"):
        print("OCR only (no fallback, no LLM)")
        print(f"{'config':15} {'docs':>4} {'word F1':>8} {'recall':>7} {'table cells':>12} {'mean s':>7} {'median s':>9} {'warm-up s':>9}")
        for name, run in report["ocr_only"].items():
            s = run["summary"]
            print(f"{name:15} {s['documents']:>4} {s['word_f1_mean']!s:>8} {s['word_recall_mean']!s:>7} "
                  f"{s['table_cell_accuracy']!s:>12} {s['seconds_mean']!s:>7} {s['seconds_median']!s:>9} "
                  f"{report['warm_up_seconds'].get(name)!s:>9}")
    if report.get("pipeline"):
        print("\nFull pipeline (OCR backend -> vlm fallback, classification, extraction, validation)")
        print(f"{'config':15} {'docs':>4} {'success':>8} {'fields':>7} {'items F1':>9} {'tables':>7} "
              f"{'mean s':>7} {'median s':>9} {'vlm pages':>10} {'vlm docs':>9} {'LLM':>5} {'tmpl':>5} {'review':>7} {'false-ok':>9}")
        for name, run in report["pipeline"].items():
            s = run["summary"]
            print(f"{name:15} {s['documents']:>4} {s['document_success_rate']!s:>8} {s['field_accuracy']!s:>7} "
                  f"{s['line_items']['f1']!s:>9} {s['table_cell_accuracy']!s:>7} {s['seconds_mean']!s:>7} "
                  f"{s['seconds_median']!s:>9} {s['vlm_page_share']!s:>10} {s['vlm_document_share']!s:>9} "
                  f"{s['llm_calls']:>5} {s['templates']['documents']:>5} {s['needs_review']:>7} "
                  f"{s['false_successes']}/{s['silent_successes']:>4}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--configs", nargs="+", choices=list(CONFIGS), default=list(CONFIGS))
    parser.add_argument("--ocr-only", action="store_true", help="skip the full-pipeline runs (no LLM calls)")
    parser.add_argument("--pipeline-only", action="store_true", help="skip the OCR-only runs")
    parser.add_argument("--dataset", action="append", type=Path, help="directory to use (repeatable); default both eval sets")
    parser.add_argument("--limit", type=int, help="first N documents only (smoke test)")
    parser.add_argument("--no-layout-markers", action="store_true",
                        help="give the LLM page text without [TABLE n] / [COLUMN n] markers (DOCKET_LAYOUT_MARKERS=false)")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "results" / "ocr_benchmark.json")
    parser.add_argument("--checkpoint", type=Path, metavar="JSONL",
                        help="append each finished pipeline document here and skip those already in it, "
                             "so an interrupted run resumes with the same command")
    args = parser.parse_args()

    if args.no_layout_markers:
        config.LAYOUT_MARKERS = False
    config.check()
    docs = documents(args.dataset or DATASETS)[: args.limit]
    print(f"{len(docs)} scanned documents")
    report: dict = {"environment": environment(args.configs), "documents": [p.name for p, _ in docs],
                    "warm_up_seconds": {}, "ocr_only": {}, "pipeline": {}}
    for name in args.configs:
        report["warm_up_seconds"][name] = warm_up(name)
        if not args.pipeline_only:
            print(f"ocr-only: {name}", flush=True)
            report["ocr_only"][name] = run_ocr_only(name, docs)
        if not args.ocr_only:
            print(f"pipeline: {name}", flush=True)
            report["pipeline"][name] = run_pipeline(name, docs, args.checkpoint)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_summary(report)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
