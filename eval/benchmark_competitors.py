"""docket against the pip-installable competition on the same eval corpus.

    /tmp/competitors/bin/python eval/benchmark_competitors.py --tools invoice2data docpick ocrcontext
    /tmp/competitors/bin/python eval/benchmark_competitors.py --tools invoice2data --limit 5   # smoke

Runs each tool over the same documents benchmark_ocr.py uses (golden + real
samples, scans only), adapts each tool's output to the keys of docket's
expected.json, and grades with the same metrics.field_accuracy. Only
documents with field-level truth take part (donut invoices, DocILE, SROIE
receipts, golden scans of invoice/receipt type): the rest of the corpus is
doc_type-only, and docpick/ocrcontext take the schema as an input rather
than classifying, so there is nothing comparable to grade there.

Fairness rules, so the comparison means something:

- Every tool is graded only on the fields its own schema can express
  (ocrcontext's Invoice has no subtotal/tax_amount, docpick's has both —
  each is graded on its own intersection with the expected keys, and the
  table says so). docket's number for the same intersection is recomputed
  from the extended benchmark JSON, not taken wholesale.
- A tool error (crash, no template match, LLM failure) counts as every
  graded field wrong, the same way a failed document counts against docket.
- The LLM-backed tools run against the same local Ollama daemon and the
  same model docket uses (DOCPICK_LLM_MODEL / OCRCONTEXT_MODEL, default
  deepseek-v4.1-flash:cloud), at their own default temperature (0.0).

invoice2data is template-driven, so its number is really "how often one of
the built-in vendor templates matched, and how well it then read" — the
coverage column splits that out.

Requires the tools in the *running* interpreter (they are not docket
dependencies): python -m venv /tmp/competitors && pip install docpick
invoice2data 'ocrcontext[paddle,cli]' langchain-ollama. Results go to
eval/results/competitors.json; the docket side comes from
eval/results/extended_stage2.json (run benchmark_ocr.py first).
"""
from __future__ import annotations

import argparse
import json
import signal
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
sys.path.insert(0, str(EVAL))

from metrics import field_accuracy, source_of  # noqa: E402

DATASETS = [ROOT / "eval" / "golden_dataset", ROOT / "eval" / "real_samples"]
SCANS = {".jpg", ".jpeg", ".png", ".pdf"}
LLM_MODEL = "deepseek-v4.1-flash:cloud"

# Tool field -> docket expected.json key. Invoices and receipts only:
# those are the documents with field-level truth, and every tool has a
# schema for exactly these two shapes.
DOCPICK_INVOICE = {
    "invoice_number": "invoice_number", "invoice_date": "issue_date",
    "vendor_name": "seller.name", "vendor_tax_id": "seller.tax_ids[0].value",
    "customer_name": "buyer.name", "subtotal": "subtotal",
    "tax_amount": "tax_amount", "total_amount": "total_amount",
}
DOCPICK_RECEIPT = {
    "merchant_name": "merchant_name", "transaction_date": "transaction_date",
    "total": "total_amount", "subtotal": "subtotal",
}
OCRCONTEXT_INVOICE = {
    "supplier_name": "seller.name", "invoice_date": "issue_date",
    "invoice_number": "invoice_number", "tax_id": "seller.tax_ids[0].value",
    "total_amount": "total_amount",
}
OCRCONTEXT_RECEIPT = {
    "store_name": "merchant_name", "date": "transaction_date",
    "total_amount": "total_amount", "subtotal": "subtotal",
}
INVOICE2DATA = {
    "issuer": "seller.name", "invoice_number": "invoice_number", "date": "issue_date",
    "subtotal": "subtotal", "tax": "tax_amount", "amount": "total_amount",
    "buyer": "buyer.name",
}


def documents() -> list[tuple[Path, dict]]:
    """(path, expected) for every scan with a .expected.json beside it."""
    found = []
    for folder in DATASETS:
        for expected_path in sorted(folder.glob("*.expected.json")):
            stem = expected_path.name[: -len(".expected.json")]
            for candidate in sorted(folder.glob(f"{stem}.*")):
                if candidate.suffix.lower() in SCANS:
                    found.append((candidate, json.loads(expected_path.read_text())))
                    break
    return found


def expected_keys(expected: dict) -> set[str]:
    return {k for k in expected if not k.startswith("_") and k != "doc_type"}


@contextmanager
def _doc_timeout(seconds: int = 300):
    """A tool's LLM call can hang forever (ollama kept a dead cloud request
    open for 25 minutes mid-benchmark). SIGALRM turns that into a failed
    document instead of a stalled run; native OCR code finishes its current
    call first, which is fine — that part is bounded."""
    def _raise(signum, frame):
        raise TimeoutError(f"no answer in {seconds}s")
    old = signal.signal(signal.SIGALRM, _raise)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def coerce(value):
    if isinstance(value, str):
        try:
            return round(float(value.replace(",", "")), 2)
        except ValueError:
            return value.strip()
    return value


def adapt(raw: dict, field_map: dict[str, str]) -> dict:
    """Tool output -> docket expected-key space (skipping absent fields)."""
    return {dst: coerce(raw[src]) for src, dst in field_map.items()
            if raw.get(src) is not None}


def graded_expected(expected: dict, field_map: dict[str, str]) -> dict:
    return {k: v for k, v in expected.items() if k in set(field_map.values())}


def fail(path, expected, field_map, exc, started) -> dict:
    """A tool error is a document the user got nothing from: every graded
    field wrong, same as a failed document counts against docket."""
    row = grade(path, expected, None, field_map, seconds=round(time.perf_counter() - started, 2))
    row["error"] = f"{type(exc).__name__}: {exc}"
    return row


# ---- the tools --------------------------------------------------------------------------------

def run_invoice2data(docs, env):
    from invoice2data.api import extract_data
    from invoice2data.input import paddleocr as paddle_reader  # its tesseract reader needs imagemagick; paddle is in the venv anyway

    for path, expected in docs:
        if expected.get("doc_type") != "invoice" or path.suffix.lower() == ".pdf":
            yield {"document": path.name, "skipped": "invoice2data grades jpg/png invoices only"}
            continue
        started = time.perf_counter()
        try:
            raw = extract_data(str(path), input_module=paddle_reader)
            matched = bool(raw)  # its historical contract: {} means no template matched
            raw = raw or {}
        except Exception as exc:  # noqa: BLE001 — a template mismatch is an outcome, not a crash of ours
            raw, matched = {}, False
            if "NoTemplateFound" not in type(exc).__name__:
                yield fail(path, expected, INVOICE2DATA, exc, started)
                continue
        yield grade(path, expected, adapt(raw, INVOICE2DATA), INVOICE2DATA,
                    seconds=round(time.perf_counter() - started, 2), template_matched=matched)


def run_docpick(docs, env):
    import os
    os.environ.update({"DOCPICK_LLM_PROVIDER": "ollama",
                       "DOCPICK_LLM_BASE_URL": "http://localhost:11434",
                       "DOCPICK_LLM_MODEL": env["model"], "DOCPICK_LLM_TIMEOUT": "120",
                       # auto' tiers up through engines that aren't installed here
                       # (easyocr, GOT) and burns minutes per document probing them.
                       "DOCPICK_OCR_ENGINE": "paddle"})
    from docpick import DocpickPipeline
    from docpick.schemas import InvoiceSchema, ReceiptSchema

    pipeline = DocpickPipeline()
    for path, expected in docs:
        doc_type, schema, field_map = {
            "invoice": ("invoice", InvoiceSchema, DOCPICK_INVOICE),
            "receipt": ("receipt", ReceiptSchema, DOCPICK_RECEIPT),
        }.get(expected.get("doc_type"), (None, None, None))
        if schema is None:
            yield {"document": path.name, "skipped": f"no docpick schema for {expected.get('doc_type')}"}
            continue
        started = time.perf_counter()
        try:
            with _doc_timeout():
                result = pipeline.extract(str(path), schema=schema)
            raw = result.data or {}
        except Exception as exc:  # noqa: BLE001
            yield fail(path, expected, field_map, exc, started)
            continue
        yield grade(path, expected, adapt(raw, field_map), field_map,
                    seconds=round(time.perf_counter() - started, 2))


def run_ocrcontext(docs, env):
    from langchain_ollama import ChatOllama
    from ocrcontext import Analyzer
    from ocrcontext.schemas import Invoice, Receipt

    analyzer = Analyzer(llm=ChatOllama(model=env["model"], temperature=0))
    for path, expected in docs:
        schema, field_map = {
            "invoice": (Invoice, OCRCONTEXT_INVOICE),
            "receipt": (Receipt, OCRCONTEXT_RECEIPT),
        }.get(expected.get("doc_type"), (None, None))
        if schema is None:
            yield {"document": path.name, "skipped": f"no ocrcontext schema for {expected.get('doc_type')}"}
            continue
        started = time.perf_counter()
        try:
            with _doc_timeout():
                raw = analyzer.extract(str(path), schema=schema).model_dump()
        except Exception as exc:  # noqa: BLE001
            yield fail(path, expected, field_map, exc, started)
            continue
        yield grade(path, expected, adapt(raw, field_map), field_map,
                    seconds=round(time.perf_counter() - started, 2))


def grade(path, expected, extracted: dict, field_map: dict, **extra) -> dict:
    exp = graded_expected(expected, field_map)
    correct, total, mismatches = field_accuracy(extracted, exp)
    return {"document": path.name, "source": source_of(path.name),
            "fields": {"correct": correct, "total": total, "mismatches": mismatches},
            **extra}


TOOLS = {"invoice2data": run_invoice2data, "docpick": run_docpick, "ocrcontext": run_ocrcontext}


def summarize(rows: list[dict]) -> dict:
    graded = [r for r in rows if "fields" in r]
    correct = sum(r["fields"]["correct"] for r in graded)
    total = sum(r["fields"]["total"] for r in graded)
    errors = sum(1 for r in rows if r.get("error"))
    matched = [r for r in graded if r.get("template_matched", True)]
    m_correct = sum(r["fields"]["correct"] for r in matched)
    m_total = sum(r["fields"]["total"] for r in matched)
    seconds = [r["seconds"] for r in graded if r.get("seconds") is not None]
    per_source = {}
    for r in graded:
        s = per_source.setdefault(r["source"], {"correct": 0, "total": 0})
        s["correct"] += r["fields"]["correct"]
        s["total"] += r["fields"]["total"]
    return {
        "documents": len(graded), "errors": errors,
        "field_accuracy": round(correct / total, 4) if total else None,
        "fields_graded": total,
        "template_matched": len(matched),
        "field_accuracy_on_matched": round(m_correct / m_total, 4) if m_total else None,
        "seconds_mean": round(statistics.mean(seconds), 2) if seconds else None,
        "per_source": {k: {**v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] else None}
                       for k, v in per_source.items()},
    }


def docket_on_their_fields(rows: list[dict], field_map: dict[str, str]) -> dict:
    """docket's accuracy recomputed on exactly the fields the tool can express,
    from the extended benchmark's per-document rows."""
    mappable = set(field_map.values())
    correct = total = 0
    for r in rows:
        expected = r.get("expected_fields")
        if expected is None:
            continue
        graded = mappable & set(expected)
        wrong = set(r["fields"]["mismatches"]) & graded
        correct += len(graded) - len(wrong)
        total += len(graded)
    return {"field_accuracy": round(correct / total, 4) if total else None, "fields_graded": total}


def join_docket(report: dict, comparable: list[tuple[Path, dict]]) -> None:
    """docket on the same intersections, for the joined table: each tool's join
    is restricted to the documents that tool actually graded, so a subsampled
    run (--max-docs) compares like with like."""
    docket_json = ROOT / "eval" / "results" / "extended_stage2.json"
    if not docket_json.exists():
        print("\n(no eval/results/extended_stage2.json — run benchmark_ocr.py for docket's side)")
        return
    d = json.loads(docket_json.read_text())["pipeline"]["tesseract"]["documents"]
    d_by_name = {r["document"]: r for r in d}
    expected_by_name = {p.name: e for p, e in comparable}
    report["docket_on_their_fields"] = {}
    for tool, field_map in (
        ("invoice2data", INVOICE2DATA),
        ("docpick", DOCPICK_INVOICE | DOCPICK_RECEIPT),
        ("ocrcontext", OCRCONTEXT_INVOICE | OCRCONTEXT_RECEIPT),
    ):
        rows = []
        for r in report.get(tool, {}).get("documents", []):
            if "fields" not in r or r["document"] not in d_by_name:
                continue
            expected = expected_by_name[r["document"]]
            rows.append({**d_by_name[r["document"]], "expected_fields": sorted(expected_keys(expected))})
        report["docket_on_their_fields"][tool] = docket_on_their_fields(rows, field_map)


def rejoin(paths: list[Path]) -> None:
    """Recompute docket's side of saved competitor reports from the current
    extended_stage2.json, without running the tools again (their versions and
    model are pinned, so a new docket release only needs its own run)."""
    comparable = [(p, e) for p, e in documents() if expected_keys(e)]
    for path in paths:
        report = json.loads(path.read_text())
        join_docket(report, comparable)
        path.write_text(json.dumps(report, indent=2))
        for tool, joined in report.get("docket_on_their_fields", {}).items():
            if report.get(tool):
                print(f"  {path.name}: docket on {tool}'s fields -> {joined}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tools", nargs="+", choices=list(TOOLS), default=list(TOOLS))
    parser.add_argument("--limit", type=int, help="first N comparable documents only (smoke test)")
    parser.add_argument("--max-docs", type=int, dest="max_docs",
                        help="evenly-spaced subsample of the comparable set (every ceil(N/max)-th document): "
                             "for tools too slow to read the whole corpus, keeps every source represented")
    parser.add_argument("--model", default=LLM_MODEL, help="Ollama model for the LLM-backed tools")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "results" / "competitors.json")
    parser.add_argument("--rejoin", type=Path, nargs="+", metavar="REPORT",
                        help="only recompute docket's side of saved reports from extended_stage2.json")
    args = parser.parse_args()
    if args.rejoin:
        rejoin(args.rejoin)
        return

    comparable = [(p, e) for p, e in documents() if expected_keys(e)]
    if args.limit:
        comparable = comparable[: args.limit]
    if args.max_docs and len(comparable) > args.max_docs:
        stride = -(-len(comparable) // args.max_docs)  # ceil: keeps every source represented
        comparable = comparable[::stride]
        print(f"subsampled to {len(comparable)} comparable documents (stride {stride})")
    print(f"{len(comparable)} comparable documents (field-level truth)")

    report: dict = {"model": args.model, "documents": len(comparable)}
    for name in args.tools:
        print(f"\n== {name}")
        rows = []
        for row in TOOLS[name](comparable, {"model": args.model}):
            rows.append(row)
            if "fields" in row:
                f = row["fields"]
                print(f"  {row['document']:34s} {f['correct']}/{f['total']}"
                      f"{'  template!' if not row.get('template_matched', True) else ''}"
                      f"{'  ERR ' + row['error'][:60] if row.get('error') else ''}", flush=True)
            elif row.get("error"):
                print(f"  {row['document']:34s} ERROR {row['error'][:70]}", flush=True)
        report[name] = {"summary": summarize(rows), "documents": rows}

    join_docket(report, comparable)

    args.out.parent.mkdir(exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")
    for name in args.tools:
        s = report[name]["summary"]
        print(f"  {name:12} docs={s['documents']} fields={s['fields_graded']} "
              f"accuracy={s['field_accuracy']} errors={s['errors']} mean_s={s['seconds_mean']}")


if __name__ == "__main__":
    main()