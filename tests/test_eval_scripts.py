"""The eval scripts are what a reader is told to run, so they have to at
least survive contact with the current code.

This exists because `eval/benchmark_methods.py` — the command the README
gives for reproducing its headline comparison table — crashed with a
`ValueError: too many values to unpack` after `_ocr_image` grew a third
return value. Nothing caught it: no test imported the script, and the
README kept advertising it. A reviewer following the instructions would
have hit a traceback.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((ROOT / "eval").glob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_eval_script_parses(script):
    ast.parse(script.read_text(encoding="utf-8"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_eval_script_imports_cleanly(script):
    """Import the module without running main(). Catches a script that
    references something the library no longer exports."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import importlib.util,sys; sys.path.insert(0, {str(ROOT / 'src')!r}); "
                f"sys.path.insert(0, {str(ROOT / 'eval')!r}); "
                f"spec=importlib.util.spec_from_file_location('m', {str(script)!r}); "
                f"m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1500:]


def test_benchmark_survives_the_ocr_call_it_makes():
    """The bug this file exists for was runtime, not import-time: the
    benchmark unpacked two values from the OCR helper after it had grown a
    third. Importing the module could never catch that, so the OCR path is
    exercised directly with the vision call stubbed out.
    """
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "eval"))
    import benchmark_methods

    benchmark_methods.vision_transcribe = lambda _png: "transcribed"
    rows = benchmark_methods._bench_ocr(
        [ROOT / "eval" / "golden_dataset" / "receipt_scan.png"]
    )

    assert rows and rows[0]["tesseract_chars"] > 0
    assert "vlm_latency_s" in rows[0]


def test_readme_only_advertises_scripts_that_exist():
    """A command in the README that doesn't resolve is the same failure in a
    different place."""
    for doc in ("README.md", "docs/BENCHMARKS.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for name in set(re.findall(r"python eval/([\w./-]+\.py)", text)):
            assert (ROOT / "eval" / name).exists(), f"{doc} runs a missing script: {name}"


def test_golden_dataset_covers_every_builtin_schema():
    import json

    sys.path.insert(0, str(ROOT / "src"))
    from docket.catalog import list_schemas

    golden = ROOT / "eval" / "golden_dataset"
    types = {json.loads(p.read_text(encoding="utf-8")).get("doc_type") for p in golden.glob("*.expected.json")}
    builtin = {s.schema_id for s in list_schemas() if s.builtin}
    assert builtin <= types, sorted(builtin - types)


def test_golden_scans_have_complete_ground_truth():
    import json

    golden = ROOT / "eval" / "golden_dataset"
    # receipt_scan predates the generator and has no text or table ground truth.
    scans = sorted(p for p in golden.glob("*_scan.expected.json") if p.name != "receipt_scan.expected.json")
    assert len(scans) >= 13
    for path in scans:
        expected = json.loads(path.read_text(encoding="utf-8"))
        document = [p for p in golden.glob(path.name.replace(".expected.json", ".*")) if p != path]
        assert len(document) == 1, path.name
        assert expected["_text"] and expected["doc_type"] == expected["_schema"]
        for table in expected["_tables"]:
            assert table["page"] >= 1 and all(len(r) == len(table["cells"][0]) for r in table["cells"])
        printed = " ".join(expected["_text"])
        for item in expected["_line_items"]:
            assert item["description"] in printed, (path.name, item["description"])


def test_line_item_precision_only_grades_documents_with_markup():
    """Real samples have no line-item ground truth, so extracted items there
    are neither right nor wrong — counting them as false positives cut the
    reported precision in half. Precision must only look at graded docs.
    """
    import sys

    sys.path.insert(0, str(ROOT / "eval"))
    from benchmark_ocr import summarize

    def row(extracted, expected):
        return {"seconds": 1.0, "acquire_seconds": 0.5, "fields": {"correct": 1, "total": 1},
                "line_items": {"correct": min(extracted, expected), "extracted": extracted, "expected": expected},
                "table_cells": {"matched": 0, "expected": 0}, "pages": 1, "vlm_pages": 0,
                "escalated_to_vlm": False, "llm_calls": 1, "needs_review": False, "success": True,
                "status": "succeeded"}

    s = summarize([row(2, 3), row(7, 0)])  # second doc: real sample, ungraded
    assert s["line_items"]["precision"] == 1.0
    assert s["line_items"]["recall"] == round(2 / 3, 4)
    assert s["line_items"]["documents_graded"] == 1


def test_false_success_rate_counts_only_silent_clean_claims():
    """A false success is a wrong answer nobody was told to check. Docs that
    went to review or failed made no clean claim, so they stay out of the
    denominator — the rate answers 'when docket says nothing is wrong, how
    often is it?'."""
    import sys

    sys.path.insert(0, str(ROOT / "eval"))
    from benchmark_ocr import summarize

    def row(status, success, review=False):
        return {"seconds": 1.0, "acquire_seconds": 0.5, "fields": {"correct": 1, "total": 2},
                "line_items": {"correct": 0, "extracted": 0, "expected": 0},
                "table_cells": {"matched": 0, "expected": 0}, "pages": 1, "vlm_pages": 0,
                "escalated_to_vlm": False, "llm_calls": 1, "needs_review": review, "success": success,
                "status": status}

    s = summarize([
        row("succeeded", True),                    # clean claim, right
        row("succeeded", False),                   # clean claim, wrong — the false success
        row("succeeded", False, review=True),     # caught: review means no clean claim
        row("failed", False),                      # no claim at all
    ])
    assert s["silent_successes"] == 2
    assert s["false_successes"] == 1
    assert s["false_success_rate"] == 0.5


def test_ocr_benchmark_reports_template_usage_and_minimum_saved_calls():
    import sys

    sys.path.insert(0, str(ROOT / "eval"))
    from benchmark_ocr import summarize

    def row(template_id=None, seconds=1.0):
        return {"seconds": seconds, "acquire_seconds": 0.5, "fields": {"correct": 1, "total": 1},
                "line_items": {"correct": 0, "extracted": 0, "expected": 0},
                "table_cells": {"matched": 0, "expected": 0}, "pages": 1, "vlm_pages": 0,
                "escalated_to_vlm": False, "llm_calls": 0 if template_id else 1,
                "template_id": template_id, "needs_review": False, "success": True,
                "status": "succeeded"}

    s = summarize([row("acme", 0.2), row(), row("acme", 0.4)])
    assert s["templates"] == {
        "documents": 2,
        "hit_rate": round(2 / 3, 4),
        "seconds_mean": 0.3,
        "llm_extraction_calls_avoided_minimum": 2,
        "by_id": {"acme": 2},
    }


def test_variance_summary_counts_missing_fields_as_disagreement():
    import sys

    sys.path.insert(0, str(ROOT / "eval"))
    from benchmark_variance import summarize_variance

    runs = [{"status": "succeeded", "needs_review": False, "seconds": 1.0,
             "extracted": {"total_amount": 32.7, "discount_amount": 2.0}},
            {"status": "succeeded", "needs_review": False, "seconds": 1.0,
             "extracted": {"total_amount": 32.7}}]
    s = summarize_variance(runs)
    assert s["agreement"] == 0.5  # total_amount agrees, discount_amount missing in run 2
    assert s["per_field"]["total_amount"]["values"] == [[32.7, 2]]


def test_pipeline_benchmark_resumes_from_its_checkpoint(tmp_path, monkeypatch):
    """A long run killed half-way must continue where it stopped, not restart."""
    sys.path.insert(0, str(ROOT / "eval"))
    import benchmark_ocr

    from tests.factories import make_result

    calls = []

    def fake_process(path, options):
        calls.append(path.name)
        return make_result(source=str(path))

    monkeypatch.setattr(benchmark_ocr, "process_document", fake_process)
    docs = [(tmp_path / f"doc{i}.png", {"doc_type": "invoice", "invoice_number": "INV-1"}) for i in range(3)]
    checkpoint = tmp_path / "run.jsonl"

    first = benchmark_ocr.run_pipeline("tesseract", docs[:2], checkpoint)
    assert calls == ["doc0.png", "doc1.png"] and len(checkpoint.read_text().splitlines()) == 2
    second = benchmark_ocr.run_pipeline("tesseract", docs, checkpoint)
    assert calls == ["doc0.png", "doc1.png", "doc2.png"]  # only the new one ran
    assert [r["document"] for r in second["documents"]] == ["doc0.png", "doc1.png", "doc2.png"]
    assert second["documents"][:2] == first["documents"]
    assert benchmark_ocr.run_pipeline("paddle-mobile", docs[:1], checkpoint)["documents"]  # per config
    assert calls[-1] == "doc0.png"


def test_pipeline_benchmark_records_source_and_key_field_confidence(tmp_path, monkeypatch):
    """One run must hold what tuning DOCKET_MIN_SOURCE_CONFIDENCE needs."""
    sys.path.insert(0, str(ROOT / "eval"))
    from datetime import date

    import benchmark_ocr

    from docket.result import SourceLocation
    from tests.factories import flat_invoice, make_result

    invoice = flat_invoice(invoice_number="INV-1", issue_date=date(2026, 3, 1), vendor_name="A",
                           customer_name="B", subtotal=18.0, total_amount=18.0)
    sources = {
        "total_amount": SourceLocation(page=1, quote="Total 18.00", confidence=0.42),
        "invoice_number": SourceLocation(page=1, quote="INV-1"),
    }
    monkeypatch.setattr(benchmark_ocr, "process_document", lambda path, options: make_result(
        source=str(path), field_sources=sources, extracted=invoice.model_dump(mode="json", exclude={"field_locations"})))
    docs = [(tmp_path / "receipt_sroie_03.jpg", {"doc_type": "invoice"}), (tmp_path / "x_scan.png", {"doc_type": "invoice"})]

    rows = benchmark_ocr.run_pipeline("tesseract", docs)["documents"]

    assert [r["source"] for r in rows] == ["sroie", "golden"]
    assert rows[0]["key_source_confidence"] == {"total_amount": 0.42, "invoice_number": None}

