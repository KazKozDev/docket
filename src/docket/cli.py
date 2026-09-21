"""Command-line interface.

    docket process FILE  [--export FORMAT | --format json|jsonl|csv] [--output PATH]
    docket batch INPUT   [--recursive] [--glob PATTERN] [--workers N] [--fail-fast]
                         [--format json|jsonl|csv] [--output PATH] [--checkpoint PATH]
    docket schemas list | show ID | json-schema ID
    docket formats
    docket ocr-backends
    docket forensics FILE

Both processing commands take the OCR (--ocr-backend, --ocr-fallback, ...)
and schema (--document-type, --schema, --schema-version) options, and
leave page layouts out of the output unless --include-layout.

Exit codes:
  0  every document succeeded
  1  partial success: some documents failed or need review (for `process`:
     the document needs review)
  2  every document failed (for `process`: the document failed)
  3  configuration error — nothing was processed
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, catalog
from .errors import ConfigurationError
from .export import ExportError, export_document, list_exporters
from .ocr import list_ocr_backends
from .options import OcrOptions, ProcessOptions
from .pipeline import process_document

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2
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
    output = parser.add_argument_group("output")
    output.add_argument("--output", "-o", metavar="PATH", help="Write results here instead of stdout")
    output.add_argument("--format", choices=["json", "jsonl", "csv"], default="json",
                        help="json (default), jsonl (one result per line) or csv (summary; see --line-items)")
    output.add_argument("--line-items", metavar="PATH",
                        help="csv: also write line items here (default: <output>.line_items.csv when --output is set)")
    output.add_argument("--include-layout", action="store_true",
                        help="Include page layouts (words, lines, tables) in json/jsonl output")


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
        include_layout=args.include_layout,
    )


class _Sink:
    """Streams results to the chosen format as they arrive, in input order."""

    def __init__(self, args: argparse.Namespace):
        from .export import tabular

        self.args = args
        self.tabular = tabular
        self.stream = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
        self.items_stream = None
        self.count = 0
        if args.format == "csv":
            self.summary = tabular.CsvWriter(self.stream, tabular.RESULT_COLUMNS)
            items_path = args.line_items or (f"{Path(args.output).with_suffix('')}.line_items.csv" if args.output else None)
            if items_path:
                self.items_stream = open(items_path, "w", encoding="utf-8", newline="")
                self.items = tabular.CsvWriter(self.items_stream, tabular.ITEM_COLUMNS)
        elif args.format == "json":
            self.stream.write("[")

    def write(self, result) -> None:
        fmt = self.args.format
        if fmt == "csv":
            self.summary.write(self.tabular.result_row(result))
            if self.items_stream is not None:
                for row in self.tabular.line_item_rows(result):
                    self.items.write(row)
        else:
            line = self.tabular.jsonl_line(result, include_layout=self.args.include_layout).rstrip("\n")
            if fmt == "jsonl":
                self.stream.write(line + "\n")
            else:
                self.stream.write(("," if self.count else "") + "\n" + line)
        self.count += 1
        self.stream.flush()

    def close(self) -> None:
        if self.args.format == "json":
            self.stream.write("\n]\n" if self.count else "]\n")
        if self.stream is not sys.stdout:
            self.stream.close()
        if self.items_stream is not None:
            self.items_stream.close()


def _cmd_process(args: argparse.Namespace) -> int:
    result = process_document(args.document, _options(args))
    if args.export:
        try:
            content = export_document(result, args.export).content
        except ExportError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_FAILED if result.status.value == "failed" else EXIT_PARTIAL
        if args.output:
            Path(args.output).write_text(content, encoding="utf-8")
        else:
            print(content)
        return EXIT_OK
    if args.format == "json":
        text = json.dumps(
            result.model_dump(mode="json", exclude=None if args.include_layout else {"layout": True}),
            indent=2,
            ensure_ascii=False,
        )
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
    else:
        sink = _Sink(args)
        sink.write(result)
        sink.close()
    return {"succeeded": EXIT_OK, "needs_review": EXIT_PARTIAL}.get(result.status.value, EXIT_FAILED)


def _cmd_batch(args: argparse.Namespace) -> int:
    from .batch import BatchOptions, process_batch

    from .options import resolve

    import glob as globlib

    options = resolve(_options(args))  # configuration errors before any file is opened
    if not Path(args.input).exists() and not globlib.has_magic(args.input):
        raise ConfigurationError(f"input {args.input!r} does not exist")
    checkpoint = args.checkpoint or (f"{Path(args.output).with_suffix('')}.checkpoint.jsonl" if args.output else None)
    batch_options = BatchOptions(
        recursive=args.recursive,
        glob=args.glob,
        workers=args.workers,
        fail_fast=args.fail_fast,
        checkpoint=checkpoint,
        keep_results=False,
    )
    sink = _Sink(args)
    try:
        batch = process_batch(args.input, options, batch_options, on_result=lambda _i, r: sink.write(r))
    finally:
        sink.close()
    if batch.total == 0:
        raise ConfigurationError(f"no supported documents found in {args.input!r}")
    print(
        f"{batch.total} documents: {batch.succeeded} succeeded, {batch.needs_review} need review, "
        f"{batch.failed} failed, {batch.skipped} skipped, {batch.metrics.documents_resumed} resumed "
        f"— {batch.elapsed_seconds:.1f}s",
        file=sys.stderr,
    )
    for error in batch.errors:
        print(f"  failed: {error.source} [{error.code}] {error.message}", file=sys.stderr)
    if batch.succeeded == batch.total:
        return EXIT_OK
    if batch.failed == batch.total:
        return EXIT_FAILED
    return EXIT_PARTIAL


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

    batch = commands.add_parser("batch", help="Process a directory, glob or list of documents")
    batch.add_argument("input", help="A directory, a glob pattern ('scans/**/*.pdf') or one file")
    batch.add_argument("--recursive", "-r", action="store_true", help="Descend into subdirectories")
    batch.add_argument("--glob", metavar="PATTERN", help="Filename pattern inside a directory, e.g. '*.pdf'")
    batch.add_argument("--workers", type=int, metavar="N", help="Documents in flight; default DOCKET_BATCH_WORKERS")
    batch.add_argument("--fail-fast", action="store_true", help="Stop after the first failed document")
    batch.add_argument("--checkpoint", metavar="PATH",
                       help="Resume file (JSON Lines); default <output>.checkpoint.jsonl when --output is set")
    _add_processing_options(batch)
    batch.set_defaults(func=_cmd_batch)

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
