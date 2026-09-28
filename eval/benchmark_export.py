"""Do the invoices docket read export as valid e-invoices? No LLM.

    python eval/benchmark_ocr.py --pipeline-only --dataset eval/golden_dataset \\
        --save-results eval/results/golden_results --out eval/results/golden_pipeline.json
    python eval/benchmark_export.py eval/results/golden_results [OUT.json]

Every saved `DocumentResult` of an invoice or credit note is exported to each
built-in e-invoice format that accepts its type, and the output is checked
with the official rules (XSD + Schematron, `validate_einvoice=True`). Each
attempt ends one of three ways:

- valid: exported and passed the official validation;
- invalid: exported, but the official rules report errors;
- refused: docket would not export it (the result needs review, or the
  document cannot be represented in that format).

Needs the [einvoice] extra and `docket einvoice fetch`. Results go to
eval/results/benchmark_export.json.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docket import (
    DocumentResult,
    ExportError,
    ExportOptions,
    export_document,
    list_exporters,
)

OUT = ROOT / "eval" / "results" / "benchmark_export.json"


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "eval" / "results" / "golden_results"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    results = [DocumentResult.model_validate_json(p.read_text(encoding="utf-8"))
               for p in sorted(folder.glob("*.result.json"))]
    einvoice = [e for e in list_exporters() if e.einvoice_profile]
    rows = []
    for result in results:
        if result.document is None:
            continue
        for exporter in einvoice:
            if not isinstance(result.document, exporter.accepts):
                continue
            row = {"document": Path(result.source).name, "format": exporter.name, "document_status": result.status.value}
            try:
                exported = export_document(result, exporter.name, ExportOptions(validate_einvoice=True))
            except ExportError as exc:
                row.update(outcome="refused", detail=str(exc))
            else:
                report = exported.einvoice_validation
                errors = [i for i in (report.issues if report else []) if i.severity in ("fatal", "error")]
                row.update(outcome="valid" if report and report.valid else "invalid",
                           errors=[f"{i.code}: {i.message}" for i in errors])
            rows.append(row)
            print(f"{row['document']:30} {row['format']:18} {row['outcome']:8} "
                  + (row.get("detail", "") or "; ".join(row.get("errors", [])[:2]))[:110])

    by_format = {}
    for name in dict.fromkeys(r["format"] for r in rows):
        by_format[name] = dict(Counter(r["outcome"] for r in rows if r["format"] == name))
    summary = {"documents": len({r["document"] for r in rows}), "attempts": len(rows),
               "outcomes": dict(Counter(r["outcome"] for r in rows)), "by_format": by_format}
    print(f"\n{summary['documents']} documents, {summary['attempts']} exports: {summary['outcomes']}")
    for name, counts in by_format.items():
        print(f"  {name:18} {counts}")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "exports": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
