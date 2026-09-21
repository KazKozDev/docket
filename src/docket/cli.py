"""Command-line entry point: `docket path/to/document.pdf [--export <format>]`."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .export import ExportError, get_exporter, list_exporters
from .pipeline import process


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Extract and validate documents with Docket, optionally exporting to ERP or e-Invoicing formats."
    )
    parser.add_argument(
        "document", nargs="?", help="Path to input document (PDF, PNG, JPG, etc.)"
    )
    parser.add_argument(
        "--forensics",
        action="store_true",
        help="Run computer vision stamp and signature detection",
    )
    parser.add_argument(
        "--export",
        choices=[e.name for e in list_exporters()],
        metavar="FORMAT",
        help="Export the validated document; see --list-formats",
    )
    parser.add_argument(
        "--list-formats",
        action="store_true",
        help="List available export formats and exit",
    )
    parser.add_argument("--version", action="version", version=f"docket {__version__}")

    args = parser.parse_args(argv)

    if args.list_formats:
        for exporter in list_exporters():
            accepts = ", ".join(t.__name__ for t in exporter.accepts)
            print(f"{exporter.name:16} {exporter.description} [{accepts}]")
        return
    if args.document is None:
        parser.error("the following arguments are required: document")

    if args.forensics:
        from .forensics import analyze_document_forensics

        report = analyze_document_forensics(args.document)
        print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
        if report.is_empty_template or report.alterations_detected:
            raise SystemExit(2)
        return

    result = process(args.document)
    if not args.export:
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        if not result.is_valid:
            raise SystemExit(2)
        return

    doc = result.document
    if doc is None:
        print("Error: Extraction failed, cannot export.", file=sys.stderr)
        raise SystemExit(2)

    try:
        print(get_exporter(args.export)(doc))
    except ExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
