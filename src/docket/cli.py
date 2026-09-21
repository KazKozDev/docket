"""Command-line interface.

    docket process FILE [--document-type ID] [--schema MODULE:CLASS] [--export FORMAT]
    docket schemas list | show ID | json-schema ID
    docket formats
    docket ocr-backends
    docket forensics FILE

Exit codes: 0 success, 2 the document failed or did not validate,
3 configuration error (unknown backend, schema, language, ...).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, catalog
from .errors import ConfigurationError
from .export import ExportError, export_document, list_exporters
from .ocr import list_ocr_backends
from .options import OcrOptions, ProcessOptions
from .pipeline import process_document

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_CONFIG = 3


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _add_processing_options(parser: argparse.ArgumentParser) -> None:
    ocr = parser.add_argument_group("OCR")
    ocr.add_argument("--ocr-backend", metavar="NAME",
                     help="Primary OCR backend (tesseract, paddle, auto, or a plugin); default DOCKET_OCR_BACKEND")
    ocr.add_argument("--ocr-fallback", action="append", metavar="NAME",
                     help="Fallback OCR backend, tried in order; repeat for more. Default DOCKET_OCR_FALLBACKS")
    ocr.add_argument("--no-ocr-fallback", action="store_true", help="Use no fallback backend at all")
    ocr.add_argument("--ocr-languages", metavar="CODES",
                     help="ISO 639-1 codes, comma-separated (e.g. en,de); default DOCKET_OCR_LANGUAGES")
    schema = parser.add_argument_group("schema")
    schema.add_argument("--document-type", metavar="ID",
                        help="Extract as this registered schema (see `docket schemas list`); skips classification")
    schema.add_argument("--schema", metavar="MODULE:CLASS",
                        help="Extract into this Pydantic model (importable path); skips classification")
    schema.add_argument("--schema-version", metavar="VERSION",
                        help="Registered version of --document-type; default the latest")
    parser.add_argument("--no-layout", action="store_true",
                        help="Leave page layouts out of the JSON result (field locations are kept)")


def _options(args: argparse.Namespace) -> ProcessOptions:
    return ProcessOptions(
        ocr=OcrOptions(
            backend=args.ocr_backend,
            fallbacks=[] if args.no_ocr_fallback else args.ocr_fallback,
            languages=args.ocr_languages,
        ),
        document_type=args.document_type,
        schema_model=catalog.load_schema(args.schema) if args.schema else None,
        schema_version=args.schema_version,
        include_layout=not args.no_layout,
    )


def _cmd_process(args: argparse.Namespace) -> int:
    result = process_document(args.document, _options(args))
    if not args.export:
        _print_json(result.model_dump(mode="json"))
        return EXIT_OK if result.is_valid else EXIT_INVALID
    try:
        print(export_document(result, args.export).content)
    except ExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    return EXIT_OK


def _cmd_schemas(args: argparse.Namespace) -> int:
    if args.action == "list":
        specs = catalog.list_schemas(all_versions=args.all_versions)
        if args.json:
            _print_json([s.info().model_dump() for s in specs])
            return EXIT_OK
        for spec in specs:
            origin = "built-in" if spec.builtin else "custom"
            print(f"{spec.schema_id:22} {spec.version:6} {spec.status:12} {origin:8} {spec.description}")
        return EXIT_OK
    if args.schema_id is None:
        raise ConfigurationError(f"`docket schemas {args.action}` needs a schema id")
    spec = catalog.require_schema(args.schema_id, args.version)
    if args.action == "show":
        _print_json(spec.info().model_dump())
    else:
        _print_json(spec.json_schema())
    return EXIT_OK


def _cmd_formats(args: argparse.Namespace) -> int:
    for exporter in list_exporters():
        accepts = ", ".join(t.__name__ for t in exporter.accepts)
        print(f"{exporter.name:16} {exporter.media_type:18} {exporter.description} [{accepts}]")
    return EXIT_OK


def _cmd_ocr_backends(args: argparse.Namespace) -> int:
    for info in list_ocr_backends():
        state = "available" if info.status.available else f"unavailable: {info.status.reason}"
        caps = info.capabilities
        flags = (
            ",".join(k for k in ("confidence", "word_coordinates", "lines", "tables", "rotation") if getattr(caps, k))
            if caps
            else "-"
        )
        print(f"{info.name:12} {flags:48} {state}")
    return EXIT_OK


def _cmd_forensics(args: argparse.Namespace) -> int:
    from .forensics import analyze_document_forensics

    report = analyze_document_forensics(args.document)
    _print_json(report.model_dump(mode="json"))
    return EXIT_INVALID if report.is_empty_template or report.alterations_detected else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docket",
        description="Extract, validate and export structured data from business documents.",
    )
    parser.add_argument("--version", action="version", version=f"docket {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    process = commands.add_parser("process", help="Process one document into a JSON result")
    process.add_argument("document", help="PDF, image (PNG/JPG/TIFF/BMP/WebP) or text file")
    process.add_argument("--export", metavar="FORMAT",
                         help="Print the document in this format instead of JSON; see `docket formats`")
    _add_processing_options(process)
    process.set_defaults(func=_cmd_process)

    schemas = commands.add_parser("schemas", help="List schemas, show one's metadata or JSON Schema")
    schemas.add_argument("action", choices=["list", "show", "json-schema"])
    schemas.add_argument("schema_id", nargs="?", help="Schema id for show / json-schema")
    schemas.add_argument("--version", dest="version", metavar="VERSION", help="A specific registered version")
    schemas.add_argument("--all-versions", action="store_true", help="list: every registered version")
    schemas.add_argument("--json", action="store_true", help="list: machine-readable output")
    schemas.set_defaults(func=_cmd_schemas)

    formats = commands.add_parser("formats", help="List export formats")
    formats.set_defaults(func=_cmd_formats)

    backends = commands.add_parser("ocr-backends", help="List OCR backends, capabilities and availability")
    backends.set_defaults(func=_cmd_ocr_backends)

    forensics = commands.add_parser("forensics", help="Stamp, signature and alteration heuristics for one file")
    forensics.add_argument("document")
    forensics.set_defaults(func=_cmd_forensics)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = args.func(args)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        code = EXIT_CONFIG
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
