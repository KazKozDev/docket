"""Command-line interface.

    docket process FILE  [--export FORMAT | --format json|jsonl|csv] [--output PATH]
    docket batch INPUT   [--recursive] [--glob PATTERN] [--workers N] [--fail-fast]
                         [--format json|jsonl|csv] [--output PATH] [--checkpoint PATH]
    docket schemas list | show ID | json-schema ID
    docket templates list | show ID
    docket formats
    docket ocr-backends
    docket validate-einvoice FILE [--profile PROFILE] [--format text|json]
    docket einvoice status | fetch [ARTIFACT ...] [--force]
    docket factur-x create PDF XML --output PDF [--level LEVEL] [--verapdf PATH]
    docket factur-x extract PDF --output XML
    docket factur-x validate PDF [--xml XML] [--verapdf PATH] [--format text|json]
    docket config show [--format text|json] | check

`docket --config PATH COMMAND ...` reads settings from a TOML file (else
DOCKET_CONFIG, else ./docket.toml); environment variables override the file
and command-line options override both. Invalid settings stop every command
except `config` before anything is read.

Both processing commands take the OCR (--ocr-backend, --ocr-fallback, ...)
and schema (--document-type, --schema, --schema-version) options;
--include-layout / --no-include-layout override DOCKET_INCLUDE_LAYOUT.

Exit codes:
  0  every document succeeded
  1  partial success: some documents failed or need review (for `process`:
     the document needs review)
  2  every document failed (for `process`: the document failed)
  3  configuration error — nothing was processed
For `validate-einvoice`: 0 valid, 2 invalid, 3 configuration error.
For `einvoice fetch`: 0 downloaded or already present, 2 download failed.
For `config check`: 0 valid, 3 invalid.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, catalog, config
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
                     help="Primary OCR backend (tesseract, paddle, docling, auto, or a plugin); default DOCKET_OCR_BACKEND")
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
    output.add_argument("--include-layout", action=argparse.BooleanOptionalAction, default=None,
                        help="Keep page layouts (words, lines, tables) in results; default DOCKET_INCLUDE_LAYOUT")


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
        from .export import ExportOptions

        try:
            exported = export_document(result, args.export, ExportOptions(validate_einvoice=args.validate_export))
        except ExportError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_FAILED if result.status.value == "failed" else EXIT_PARTIAL
        if args.output:
            Path(args.output).write_text(exported.content, encoding="utf-8")
        else:
            print(exported.content)
        return _print_export_validation(exported)
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


def _cmd_templates(args: argparse.Namespace) -> int:
    from .templates import get_vendor_template, list_vendor_templates

    if args.action == "list":
        templates = list_vendor_templates()
        if args.json:
            _print_json([template.model_dump(mode="json") for template in templates])
            return EXIT_OK
        for template in templates:
            origin = "built-in" if template.builtin else "custom"
            print(f"{template.template_id:40} {template.schema_id:18} {origin:8} {template.description}")
        return EXIT_OK
    if args.template_id is None:
        raise ConfigurationError("`docket templates show` needs a template id")
    template = get_vendor_template(args.template_id)
    if template is None:
        raise ConfigurationError(f"unknown vendor template {args.template_id!r}")
    _print_json(template.model_dump(mode="json"))
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


def _cmd_validate_einvoice(args: argparse.Namespace) -> int:
    from .einvoice import EInvoiceValidationOptions, Profile, validate_einvoice

    options = EInvoiceValidationOptions(profile=Profile(args.profile) if args.profile else None)
    report = validate_einvoice(Path(args.document), options)
    if args.format == "json":
        _print_json(report.model_dump(mode="json"))
    else:
        verdict = "VALID" if report.valid else "INVALID"
        print(f"{verdict}  {args.document}  {report.detected_format or '?'}  profile={report.profile.value if report.profile else '?'}")
        for layer in report.layers:
            state = "skipped: " + (layer.skipped_reason or "") if not layer.ran else ("passed" if layer.passed else "failed")
            print(f"  {layer.layer:10} {layer.artifact}  {state}")
        for issue in report.issues:
            print(f"  [{issue.severity}] {issue.code} {issue.message}")
            if issue.location:
                print(f"      at {issue.location}")
        print(f"  rules: {report.validation_resource_version}")
    return EXIT_OK if report.valid else EXIT_FAILED


def _cmd_einvoice(args: argparse.Namespace) -> int:
    from .einvoice import artifacts
    from .einvoice.fetch import downloadable, fetch_einvoice_resources

    if args.action == "fetch":
        wanted = args.artifact or downloadable()
        print(f"Downloading {', '.join(wanted)} from their official upstream releases into "
              f"{artifacts.download_root()}; their upstream terms apply (docs/THIRD_PARTY_LICENSES.md).")
        try:
            fetched = fetch_einvoice_resources(wanted, force=args.force)
        except artifacts.ArtifactError as exc:
            print(f"Download failed: {exc}", file=sys.stderr)
            return EXIT_FAILED
        print(f"downloaded: {', '.join(fetched) or 'nothing, all present'}")
        return EXIT_OK
    for entry in artifacts.manifest()["artifacts"]:
        if not entry["files"]:
            continue
        shipped = artifacts.redistributable(entry)
        state = "shipped" if shipped else ("missing" if artifacts.missing([entry["id"]]) else "downloaded")
        print(f"{entry['id']:22} {state:10} {entry['version']}")
    print(f"download root: {artifacts.download_root()}")
    return EXIT_OK


def _cmd_factur_x(args: argparse.Namespace) -> int:
    from .einvoice import (
        extract_facturx_xml,
        generate_facturx_pdf,
        verify_facturx_round_trip,
    )

    for label in ("pdf", "xml"):
        value = getattr(args, label, None)
        if value and not Path(value).is_file():
            raise ConfigurationError(f"{label.upper()} file {value!r} does not exist")

    if args.action == "create":
        generated = generate_facturx_pdf(args.pdf, args.xml, level=args.level, lang=args.lang)
        Path(args.output).write_bytes(generated)
        report = verify_facturx_round_trip(
            generated,
            expected_xml=args.xml,
            verapdf=args.verapdf,
        )
    elif args.action == "extract":
        xml = extract_facturx_xml(args.pdf)
        if args.output:
            Path(args.output).write_bytes(xml)
        else:
            sys.stdout.buffer.write(xml)
        return EXIT_OK
    else:
        report = verify_facturx_round_trip(
            args.pdf,
            expected_xml=args.xml,
            verapdf=args.verapdf,
        )
    if args.format == "json":
        _print_json(report.model_dump(mode="json"))
    else:
        print(f"{'VALID' if report.valid else 'INVALID'}  {args.output if args.action == 'create' else args.pdf}")
        print(f"  embedded XML: {'matches' if report.xml_matches else 'differs'}; official rules: "
              f"{'passed' if report.xml_validation.valid else 'failed'}")
        print(f"  PDF/A-3: {'passed' if report.pdfa_validation.compliant else 'failed'} "
              f"({report.pdfa_validation.profile}, veraPDF {report.pdfa_validation.validator_version or '?'})")
        for issue in report.pdfa_validation.issues:
            rule = "/".join(filter(None, (issue.clause, issue.test_number)))
            print(f"  [PDF/A {rule or '?'}] {issue.description}")
    return EXIT_OK if report.valid else EXIT_FAILED


def _print_export_validation(result) -> int:
    report = result.einvoice_validation
    if report is None:
        return EXIT_OK
    if report.valid:
        print(f"e-invoice valid ({report.profile.value})", file=sys.stderr)
        return EXIT_OK
    print(f"e-invoice INVALID ({report.profile.value if report.profile else '?'}):", file=sys.stderr)
    for issue in report.errors:
        print(f"  {issue.code} {issue.message}", file=sys.stderr)
    return EXIT_PARTIAL


def _cmd_config(args: argparse.Namespace) -> int:
    problems = config.ERRORS + config._semantic_errors()
    if args.action == "show":
        rows = config.describe()
        if args.format == "json":
            _print_json({"config_file": str(config.CONFIG_FILE) if config.CONFIG_FILE else None,
                         "settings": rows, "errors": problems})
        else:
            print(f"config file: {config.CONFIG_FILE or '(none)'}")
            for row in rows:
                print(f"  {row['key']:34} {row['value']!s:32} {row['source']}")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if args.action == "check" and not problems:
        print(f"ok ({config.CONFIG_FILE or 'no config file'})")
    return EXIT_CONFIG if problems else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docket",
        description="Extract, validate and export structured data from business documents.",
    )
    parser.add_argument("--version", action="version", version=f"docket {__version__}")
    parser.add_argument("--config", metavar="PATH",
                        help="TOML settings file (default: DOCKET_CONFIG, else ./docket.toml if present)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    process = commands.add_parser("process", help="Process one document into a JSON result")
    process.add_argument("document", help="PDF, image (PNG/JPG/TIFF/BMP/WebP) or text file")
    process.add_argument("--export", metavar="FORMAT",
                         help="Print the document in this format instead of JSON; see `docket formats`")
    process.add_argument("--validate-export", action="store_true",
                         help="Validate an e-invoice export with the official rules (needs the [einvoice] extra)")
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

    templates = commands.add_parser("templates", help="List vendor templates or show their extraction rules")
    templates.add_argument("action", choices=["list", "show"])
    templates.add_argument("template_id", nargs="?", help="Template id for show")
    templates.add_argument("--json", action="store_true", help="list: machine-readable output")
    templates.set_defaults(func=_cmd_templates)

    formats = commands.add_parser("formats", help="List export formats")
    formats.set_defaults(func=_cmd_formats)

    backends = commands.add_parser("ocr-backends", help="List OCR backends, capabilities and availability")
    backends.set_defaults(func=_cmd_ocr_backends)

    einvoice = commands.add_parser("validate-einvoice",
                                   help="Validate an e-invoice (XML or Factur-X PDF) with the official rules")
    einvoice.add_argument("document")
    einvoice.add_argument("--profile", choices=[p.value for p in __import__("docket.einvoice", fromlist=["Profile"]).Profile],
                          help="Validate as this profile; default: the one the document declares")
    einvoice.add_argument("--format", choices=["text", "json"], default="text")
    einvoice.set_defaults(func=_cmd_validate_einvoice)

    einvoice_resources = commands.add_parser(
        "einvoice", help="Show or download the e-invoice validation artifacts Docket does not ship")
    einvoice_actions = einvoice_resources.add_subparsers(dest="action", required=True)
    einvoice_actions.add_parser("status", help="Which artifacts are shipped, downloaded or missing")
    ei_fetch = einvoice_actions.add_parser("fetch", help="Download Peppol, CII D16B and Factur-X artifacts")
    ei_fetch.add_argument("artifact", nargs="*", help="Artifact ids (default: every one not shipped)")
    ei_fetch.add_argument("--force", action="store_true", help="Download again even when present and intact")
    einvoice_resources.set_defaults(func=_cmd_einvoice)

    factur_x = commands.add_parser("factur-x", help="Create, extract or validate a Factur-X PDF/A-3 document")
    factur_x_actions = factur_x.add_subparsers(dest="action", required=True)
    fx_create = factur_x_actions.add_parser("create", help="Embed Factur-X XML and XMP into a PDF")
    fx_create.add_argument("pdf", help="Source PDF (must itself be PDF/A compliant for a compliant result)")
    fx_create.add_argument("xml", help="Factur-X CII XML")
    fx_create.add_argument("--output", "-o", required=True, help="Generated hybrid PDF")
    fx_create.add_argument("--level", choices=["minimum", "basicwl", "basic", "en16931", "extended", "autodetect"],
                           default="autodetect")
    fx_create.add_argument("--lang", help="PDF language, for example de-DE or fr-FR")
    fx_create.add_argument("--verapdf", default="verapdf", help="Path to the veraPDF executable")
    fx_create.add_argument("--format", choices=["text", "json"], default="text")
    fx_create.set_defaults(func=_cmd_factur_x)
    fx_extract = factur_x_actions.add_parser("extract", help="Extract factur-x.xml from a hybrid PDF")
    fx_extract.add_argument("pdf")
    fx_extract.add_argument("--output", "-o", help="Write XML here instead of stdout")
    fx_extract.set_defaults(func=_cmd_factur_x)
    fx_validate = factur_x_actions.add_parser("validate", help="Run XML rules and veraPDF PDF/A-3 validation")
    fx_validate.add_argument("pdf")
    fx_validate.add_argument("--xml", help="Also require the embedded XML to match this file byte-for-byte")
    fx_validate.add_argument("--verapdf", default="verapdf", help="Path to the veraPDF executable")
    fx_validate.add_argument("--format", choices=["text", "json"], default="text")
    fx_validate.set_defaults(func=_cmd_factur_x)


    settings = commands.add_parser("config", help="Show or check the effective settings and where they come from")
    settings.add_argument("action", choices=["show", "check"])
    settings.add_argument("--format", choices=["text", "json"], default="text")
    settings.set_defaults(func=_cmd_config)
    return parser


def run() -> None:
    """Console entry point (`docket`): an application, so it also reads .env
    and ./docket.toml from the working directory."""
    main(app_files=True)


def main(argv: list[str] | None = None, *, app_files: bool = False) -> None:
    args = build_parser().parse_args(argv)
    try:
        if app_files or args.config:
            config.configure_app(args.config)
        if args.command != "config":
            config.check()
        if getattr(args, "include_layout", False) is None:
            args.include_layout = config.INCLUDE_LAYOUT
        code = args.func(args)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        code = EXIT_CONFIG
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    run()
