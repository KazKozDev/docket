"""process_batch: sources, order, partial failure, fail-fast, concurrency
limits, bounded look-ahead, checkpoint resume, CSV/JSONL output and the
`docket batch` command's exit codes."""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from pathlib import Path

import pytest

from docket import BatchOptions, ProcessOptions, ReviewOptions, config, limits, process_batch
from docket import cli
from docket import extract as extract_module
from docket.batch import iter_batch, iter_sources
from docket.export import tabular
from docket.options import OcrOptions
from docket.result import DocumentStatus
from tests.factories import make_result

INVOICE = (
    "INVOICE\nInvoice no: {n}\nDate: 2026-03-02\nFrom: Acme GmbH\nTo: Beta SA\n"
    "Widget | 2 | 50.00 | 100.00\nSubtotal: 100.00\nVAT: 21.00\nTotal: 121.00\n"
)


def _payload(n="INV-7"):
    return {
        "invoice_number": n,
        "issue_date": "2026-03-02",
        "seller": {"name": "Acme GmbH"},
        "buyer": {"name": "Beta SA"},
        "currency": "EUR",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0, "total": 100.0}],
        "subtotal": 100.0,
        "tax_amount": 21.0,
        "total_amount": 121.0,
        "field_locations": {
            "invoice_number": {"page": 1, "quote": f"Invoice no: {n}"},
            "issue_date": {"page": 1, "quote": "Date: 2026-03-02"},
            "seller.name": {"page": 1, "quote": "From: Acme GmbH"},
            "buyer.name": {"page": 1, "quote": "To: Beta SA"},
            "subtotal": {"page": 1, "quote": "Subtotal: 100.00"},
            "tax_amount": {"page": 1, "quote": "VAT: 21.00"},
            "total_amount": {"page": 1, "quote": "Total: 121.00"},
            "line_items[0].total": {"page": 1, "quote": "Widget | 2 | 50.00 | 100.00"},
        },
    }


@pytest.fixture
def options():
    return ProcessOptions(
        ocr=OcrOptions(fallbacks=[]), document_type="invoice", review=ReviewOptions(enqueue=False)
    )


@pytest.fixture
def invoices(tmp_path, monkeypatch):
    """N invoice text files; the stubbed LLM echoes each file's number."""
    def fake_chat(prompt, **_k):
        number = prompt.split("Invoice no: ")[1].split("\n")[0]
        return _payload(number)

    monkeypatch.setattr(extract_module, "chat_json", fake_chat)

    def make(n, directory=None):
        directory = directory or tmp_path / "in"
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(n):
            path = directory / f"inv{i:02d}.txt"
            path.write_text(INVOICE.format(n=f"INV-{i}"))
            paths.append(path)
        return paths

    return make


# ---- sources -------------------------------------------------------------------


def test_directory_glob_and_recursion(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    for name in ("b.pdf", "a.txt", "notes.docx", "sub/c.pdf", "sub/d.png"):
        (root / name).write_bytes(b"x")
    assert [p.name for p in iter_sources(root)] == ["a.txt", "b.pdf"]  # .docx is not a document
    assert [p.name for p in iter_sources(root, recursive=True)] == ["a.txt", "b.pdf", "c.pdf", "d.png"]
    assert [p.name for p in iter_sources(root, glob="*.pdf", recursive=True)] == ["b.pdf", "c.pdf"]
    assert [p.name for p in iter_sources(str(root / "sub" / "*.p*"))] == ["c.pdf", "d.png"]
    assert [p.name for p in iter_sources([root / "b.pdf", root / "a.txt"])] == ["b.pdf", "a.txt"]


def test_sources_are_read_lazily(options, monkeypatch):
    pulled = []

    def endless():
        n = 0
        while True:
            pulled.append(n)
            yield Path(f"/nonexistent/{n}.txt")
            n += 1

    stream = iter_batch(endless(), options, BatchOptions(workers=2))
    next(stream)
    stream.close()
    assert len(pulled) <= 2 * 2 + 1  # a bounded window, never the whole source


# ---- order, failures, fail-fast ------------------------------------------------------


def test_results_keep_input_order_whatever_finishes_first(options, invoices, monkeypatch):
    paths = invoices(6)
    from docket import pipeline

    real = pipeline.process_document

    def slow_first(path, opts):
        time.sleep(0.05 * (6 - int(path.stem[3:])))  # the first file is the slowest
        return real(path, opts)

    monkeypatch.setattr("docket.batch.process_document", slow_first)
    seen = []
    batch = process_batch(paths, options, BatchOptions(workers=6), on_result=lambda i, r: seen.append(i))
    assert [r.extracted["invoice_number"] for r in batch.results] == [f"INV-{i}" for i in range(6)]
    assert seen == list(range(6))


def test_partial_failure_does_not_stop_the_batch(options, invoices, tmp_path):
    paths = invoices(3)
    blank = tmp_path / "in" / "blank.txt"
    blank.write_text("  ")
    missing = tmp_path / "in" / "gone.pdf"
    batch = process_batch([paths[0], blank, paths[1], missing, paths[2]], options)
    assert (batch.total, batch.succeeded, batch.failed, batch.needs_review, batch.skipped) == (5, 3, 2, 0, 0)
    assert [(e.index, e.code) for e in batch.errors] == [(1, "no_text"), (3, "unreadable_file")]
    assert [r.status for r in batch.results][1] == DocumentStatus.FAILED


def test_a_crashing_document_becomes_a_failed_result(options, invoices, monkeypatch):
    paths = invoices(3)
    from docket import pipeline

    real = pipeline.process_document

    def crash_on_second(path, opts):
        if path == paths[1]:
            raise RuntimeError("segfault in a plugin")
        return real(path, opts)

    monkeypatch.setattr("docket.batch.process_document", crash_on_second)
    batch = process_batch(paths, options)
    assert batch.failed == 1 and batch.succeeded == 2
    assert batch.errors[0].code == "internal_error" and "segfault" in batch.errors[0].message


def test_fail_fast_stops_submitting(options, invoices, tmp_path):
    paths = invoices(8)
    blank = tmp_path / "in" / "blank.txt"
    blank.write_text(" ")
    batch = process_batch([blank] + paths, options, BatchOptions(workers=1, fail_fast=True))
    assert batch.failed == 1
    assert batch.stopped_early and batch.skipped >= 1
    assert batch.total == 9
    assert batch.succeeded + batch.failed + batch.skipped == 9


def test_configuration_errors_raise_before_any_document(tmp_path):
    with pytest.raises(Exception, match="unknown schema"):
        process_batch([tmp_path / "x.pdf"], ProcessOptions(document_type="nope"))


# ---- concurrency ------------------------------------------------------------------


def test_workers_bound_documents_in_flight(options, invoices, monkeypatch):
    paths = invoices(12)
    active, peak = [0], [0]
    lock = threading.Lock()

    def fake(path, opts):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return make_result(source=str(path))

    monkeypatch.setattr("docket.batch.process_document", fake)
    process_batch(paths, options, BatchOptions(workers=3))
    assert peak[0] == 3


def test_llm_calls_are_bounded_across_workers(options, invoices, monkeypatch):
    from docket import llm_client

    paths = invoices(10)
    monkeypatch.setattr(config, "LLM_CONCURRENCY", 2)

    def slow_request(payload, *, timeout):
        time.sleep(0.03)
        prompt = payload["messages"][0]["content"]
        number = prompt.split("Invoice no: ")[1].split("\n")[0]
        return json.dumps(_payload(number))

    monkeypatch.setattr(extract_module, "chat_json", llm_client.chat_json)
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(llm_client, "_ollama_request", slow_request)
    limits.reset_peaks()
    batch = process_batch(paths, options, BatchOptions(workers=8))
    assert batch.succeeded == 10
    assert limits.peak("llm") == 2
    # Each result counts only its own call: usage is per document, not shared.
    assert {r.metrics.llm_calls for r in batch.results} == {1}
    assert batch.metrics.llm_calls == 10


def test_no_temporary_files_are_left(options, invoices, tmp_path):
    import tempfile

    paths = invoices(4)
    before_in = sorted(p.name for p in (tmp_path / "in").iterdir())
    before_tmp = set(Path(tempfile.gettempdir()).iterdir())
    process_batch(paths, options, BatchOptions(workers=2))
    assert sorted(p.name for p in (tmp_path / "in").iterdir()) == before_in
    assert set(Path(tempfile.gettempdir()).iterdir()) - before_tmp == set()


# ---- metrics and resume ----------------------------------------------------------------


def test_aggregate_metrics(options, invoices):
    batch = process_batch(invoices(3), options)
    m = batch.metrics
    assert m.documents_processed == 3 and m.pages == 3
    assert m.document_seconds_mean is not None and m.document_seconds_median is not None
    assert {"acquire", "extract", "validate"} <= set(m.stage_seconds)
    assert batch.elapsed_seconds > 0


def test_checkpoint_resumes_only_unfinished_or_changed(options, invoices, tmp_path, monkeypatch):
    paths = invoices(4)
    checkpoint = tmp_path / "run.checkpoint.jsonl"
    first = process_batch(paths[:2], options, BatchOptions(checkpoint=checkpoint))
    assert first.metrics.documents_processed == 2
    assert len(checkpoint.read_text().splitlines()) == 2

    paths[1].write_text(INVOICE.format(n="INV-99"))  # changed since the first run
    from docket import pipeline

    processed = []
    real = pipeline.process_document
    monkeypatch.setattr("docket.batch.process_document", lambda p, o: processed.append(p.name) or real(p, o))
    second = process_batch(paths, options, BatchOptions(checkpoint=checkpoint))
    assert processed == ["inv01.txt", "inv02.txt", "inv03.txt"]
    assert second.metrics.documents_resumed == 1
    assert [r.extracted["invoice_number"] for r in second.results] == ["INV-0", "INV-99", "INV-2", "INV-3"]


def test_a_truncated_checkpoint_line_is_ignored(options, invoices, tmp_path):
    paths = invoices(2)
    checkpoint = tmp_path / "cp.jsonl"
    process_batch(paths, options, BatchOptions(checkpoint=checkpoint))
    checkpoint.write_text(checkpoint.read_text() + '{"source": "cut sho')
    again = process_batch(paths, options, BatchOptions(checkpoint=checkpoint))
    assert again.metrics.documents_resumed == 2


# ---- tabular output ----------------------------------------------------------------------


def test_csv_columns_are_stable_across_schemas(options, invoices):
    from docket.catalog import get_schema

    batch = process_batch(invoices(1), options)
    stream = io.StringIO()
    waybill = json.loads((Path(__file__).parent / "fixtures/catalog/waybill.expected.json").read_text())
    other = make_result(schema_id="waybill", schema_version="1.1", document_type="waybill",
                        extracted=get_schema("waybill").model.model_validate(waybill).model_dump(mode="json"))
    tabular.write_results_csv(batch.results + [other, make_result(extracted=None, schema_id="ad:hoc")], stream)
    rows = list(csv.DictReader(io.StringIO(stream.getvalue())))
    assert tuple(rows[0]) == tabular.RESULT_COLUMNS
    assert rows[0]["issuer"] == "Acme GmbH" and rows[0]["document_number"] == "INV-0"
    assert rows[1]["issuer"] == "Holzwerk Bayern GmbH" and rows[1]["document_number"] == "CMR-448120"
    assert rows[1]["recipient"] == "Bois du Nord SARL"  # same columns, filled from the waybill's own fields
    assert rows[2]["total_amount"] == ""


def test_line_items_link_back_by_document_id(options, invoices):
    batch = process_batch(invoices(2), options)
    stream = io.StringIO()
    tabular.write_line_items_csv(batch.results, stream)
    rows = list(csv.DictReader(io.StringIO(stream.getvalue())))
    assert tuple(rows[0]) == tabular.ITEM_COLUMNS
    assert [r["document_id"] for r in rows] == [r.document_id for r in batch.results]
    assert rows[0]["quantity"] == "2.0" and rows[0]["total"] == "100.0" and rows[0]["line_no"] == "1"


def test_review_reasons_are_one_cell(options):
    result = make_result(needs_review=True, review_reasons=["low confidence", "validation error: x"])
    row = tabular.result_row(result)
    assert row["review_reasons"] == "low confidence | validation error: x"


def test_jsonl_drops_layout_on_request():
    result = make_result()
    assert "layout" in json.loads(tabular.jsonl_line(result))
    assert "layout" not in json.loads(tabular.jsonl_line(result, include_layout=False))


# ---- CLI -------------------------------------------------------------------------------


def _cli(*args):
    try:
        cli.main(list(args))
    except SystemExit as exc:
        return exc.code
    return 0


@pytest.fixture
def quiet_env(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "OCR_FALLBACKS", [])


def test_cli_batch_to_csv_with_line_items(invoices, tmp_path, quiet_env, capsys):
    invoices(3)
    out = tmp_path / "out.csv"
    code = _cli("batch", str(tmp_path / "in"), "--format", "csv", "--output", str(out), "--document-type", "invoice")
    assert code == 0
    rows = list(csv.DictReader(out.open()))
    assert [r["document_number"] for r in rows] == ["INV-0", "INV-1", "INV-2"]
    items = list(csv.DictReader((tmp_path / "out.line_items.csv").open()))
    assert len(items) == 3
    assert (tmp_path / "out.checkpoint.jsonl").exists()
    assert "3 documents: 3 succeeded" in capsys.readouterr().err


def test_cli_batch_jsonl_and_json(invoices, tmp_path, quiet_env, capsys):
    invoices(2)
    assert _cli("batch", str(tmp_path / "in"), "--format", "jsonl", "--no-include-layout") == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and "layout" not in json.loads(lines[0])
    assert _cli("batch", str(tmp_path / "in"), "--format", "json", "--include-layout") == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 2 and data[0]["layout"]["pages"]


def test_cli_exit_codes(invoices, tmp_path, quiet_env, capsys):
    invoices(2)
    (tmp_path / "in" / "blank.txt").write_text(" ")
    assert _cli("batch", str(tmp_path / "in"), "--format", "jsonl") == 1  # partial
    only_blank = tmp_path / "blank"
    only_blank.mkdir()
    (only_blank / "b.txt").write_text(" ")
    assert _cli("batch", str(only_blank)) == 2  # every document failed
    assert _cli("batch", str(tmp_path / "empty-nowhere")) == 3  # nothing to process
    assert _cli("batch", str(tmp_path / "in"), "--ocr-backend", "nope") == 3  # configuration
    assert _cli("process", str(tmp_path / "in" / "blank.txt")) == 2
    assert _cli("process", str(tmp_path / "in" / "inv00.txt"), "--document-type", "invoice") == 0


def test_cli_process_needs_review_is_partial(tmp_path, quiet_env, monkeypatch):
    path = tmp_path / "doc.txt"
    path.write_text(INVOICE.format(n="INV-1"))
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _payload("INV-1") | {"total_amount": 999.0})
    assert _cli("process", str(path), "--document-type", "invoice") == 1


def test_cli_batch_resumes_from_checkpoint(invoices, tmp_path, quiet_env, monkeypatch):
    invoices(3)
    out = tmp_path / "run.jsonl"
    assert _cli("batch", str(tmp_path / "in"), "--format", "jsonl", "--output", str(out)) == 0
    calls = []
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: calls.append(1) or {})
    assert _cli("batch", str(tmp_path / "in"), "--format", "jsonl", "--output", str(out)) == 0
    assert calls == []  # everything came from the checkpoint
    assert len(out.read_text().splitlines()) == 3
