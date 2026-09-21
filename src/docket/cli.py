"""Command-line entry point: `docket path/to/document.pdf [--export <format>]`."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .doctypes import list_document_types, load_schema
from .errors import ConfigurationError
from .export import ExportError, export_document, list_exporters
from .ocr import list_ocr_backends
from .options import OcrOptions, ProcessOptions
from .pipeline import process_document


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
    parser.add_argument(
        "--list-types",
        action="store_true",
        help="List document types (built-in and registered) and exit",
    )
    parser.add_argument(
        "--ocr-backend",
        metavar="NAME",
        help="Primary OCR backend (tesseract, paddle, auto, or a plugin); default DOCKET_OCR_BACKEND",
    )
    parser.add_argument(
        "--ocr-fallback",
        action="append",
        metavar="NAME",
        help="Fallback OCR backend, tried in order; repeat for more. Default DOCKET_OCR_FALLBACKS",
    )
    parser.add_argument(
        "--no-ocr-fallback",
        action="store_true",
        help="Use no fallback backend at all",
    )
    parser.add_argument(
        "--ocr-languages",
        metavar="CODES",
        help="ISO 639-1 codes, comma-separated (e.g. en,de); default DOCKET_OCR_LANGUAGES",
    )
    parser.add_argument(
        "--document-type",
        metavar="NAME",
        help="Extract as this registered document type; skips classification",
    )
    parser.add_argument(
        "--schema",
        metavar="MODULE:CLASS",
        help="Extract into this Pydantic model (importable path); skips classification",
    )
    parser.add_argument(
        "--no-layout",
        action="store_true",
        help="Leave page layouts out of the JSON result (field locations are kept)",
    )
    parser.add_argument(
        "--list-ocr-backends",
        action="store_true",
        help="List OCR backends, their capabilities and availability, and exit",
    )
    parser.add_argument("--version", action="version", version=f"docket {__version__}")

    args = parser.parse_args(argv)

    if args.list_formats:
        for exporter in list_exporters():
            accepts = ", ".join(t.__name__ for t in exporter.accepts)
            print(f"{exporter.name:16} {exporter.description} [{accepts}]")
        return
    if args.list_types:
        for doc_type in list_document_types():
            origin = "built-in" if doc_type.builtin else "custom"
            print(f"{doc_type.name:16} {doc_type.schema.__name__:16} {origin:8} {doc_type.description}")
        return
    if args.list_ocr_backends:
        for info in list_ocr_backends():
            state = "available" if info.status.available else f"unavailable: {info.status.reason}"
            caps = info.capabilities
            flags = (
                ",".join(
                    k for k in ("confidence", "word_coordinates", "lines", "tables", "rotation")
                    if getattr(caps, k)
                )
                if caps
                else "-"
            )
            print(f"{info.name:12} {flags:48} {state}")
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

    try:
        options = ProcessOptions(
            ocr=OcrOptions(
                backend=args.ocr_backend,
                fallbacks=[] if args.no_ocr_fallback else args.ocr_fallback,
                languages=args.ocr_languages,
            ),
            document_type=args.document_type,
            schema_model=load_schema(args.schema) if args.schema else None,
            include_layout=not args.no_layout,
        )
        result = process_document(args.document, options)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc

    if not args.export:
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        if not result.is_valid:
            raise SystemExit(2)
        return

    try:
        print(export_document(result, args.export).content)
    except ExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
