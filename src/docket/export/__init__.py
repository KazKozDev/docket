"""Accounting and e-Invoicing export module for Docket.

Provides converters for electronic invoicing standards (UBL 2.1 / Peppol BIS,
Facturae 3.2.2, ZUGFeRD 2.2 / Factur-X / XRechnung). Formats for a particular
ERP or accounting system belong in the application that knows that system's
accounts and tax codes; register them as below.

Every format is also reachable by name through a small registry, which is what
the CLI uses. Applications can add their own formats either at runtime::

    from docket.export import register_exporter
    register_exporter("my-erp", my_func, accepts=(Invoice,))

or from a separate package, via a `docket.exporters` entry point whose target
is a callable that registers one or more exporters when called.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, Field

from ..catalog.models import CreditNote, Invoice
from . import en16931
from .facturae import export_to_facturae_xml

from ..einvoice.validator import EInvoiceValidationResult

if TYPE_CHECKING:
    from ..result import DocumentResult

ENTRY_POINT_GROUP = "docket.exporters"


class ExportError(ValueError):
    """The requested format is unknown or cannot represent this document."""


@dataclass(frozen=True)
class Exporter:
    name: str
    func: Callable[[Any], Any]
    accepts: tuple[type[BaseModel], ...]
    description: str = ""
    media_type: str = "text/plain"
    einvoice_profile: str | None = None  # docket.einvoice Profile value the output must satisfy

    def __call__(self, document: BaseModel) -> str:
        if not isinstance(document, self.accepts):
            wanted = " or ".join(t.__name__ for t in self.accepts)
            raise ExportError(
                f"{self.name} export requires {wanted}, got {type(document).__name__}"
            )
        try:
            output = self.func(document)
        except en16931.EN16931Error as exc:
            raise ExportError(f"{self.name}: {exc}") from exc
        if isinstance(output, str):
            return output
        return json.dumps(output, indent=2, ensure_ascii=False)


_REGISTRY: dict[str, Exporter] = {}
_plugins_loaded = False


def register_exporter(
    name: str,
    func: Callable[[Any], Any],
    *,
    accepts: tuple[type[BaseModel], ...],
    description: str = "",
    media_type: str | None = None,
    einvoice_profile: str | None = None,
    replace: bool = False,
) -> Exporter:
    """Make `func` available as export format `name`.

    `func` takes one document and returns a string, or a JSON-serializable
    object that is rendered as JSON (media type application/json by default).
    """
    if name in _REGISTRY and not replace:
        raise ExportError(f"exporter {name!r} is already registered")
    exporter = Exporter(name, func, tuple(accepts), description, media_type or "text/plain", einvoice_profile)
    _REGISTRY[name] = exporter
    return exporter


def _load_plugins() -> None:
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        ep.load()()


def list_exporters() -> list[Exporter]:
    _load_plugins()
    return list(_REGISTRY.values())


def get_exporter(name: str) -> Exporter:
    _load_plugins()
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(_REGISTRY)
        raise ExportError(f"unknown export format {name!r}; known: {known}") from None


class ExportOptions(BaseModel):
    require_valid: bool = Field(
        default=True,
        description=(
            "Refuse to export a DocumentResult that failed, has validation errors, or "
            "needs review, or a schema instance whose own checks fail. Never emit an "
            "e-invoice from unchecked data."
        ),
    )


    validate_einvoice: bool = Field(
        default=False,
        description="Validate an e-invoice format's output with the official rules (needs the [einvoice] extra).",
    )


class ExportResult(BaseModel):
    format: str
    media_type: str
    content: str
    einvoice_validation: EInvoiceValidationResult | None = None


def export_document(
    source: "DocumentResult | BaseModel", format: str, options: ExportOptions | None = None
) -> ExportResult:
    """Render a processed document in the named format.

    `source` is a DocumentResult (its typed document is exported) or a schema
    instance you built yourself. A schema instance has no source text, so
    its citations can't be checked; its arithmetic, dates and check digits
    are, and errors there refuse the export like a failed result would. To
    check an extraction made elsewhere against its document, use `verify()`.
    """
    from ..result import DocumentResult

    options = options or ExportOptions()
    exporter = get_exporter(format)
    if isinstance(source, DocumentResult):
        if options.require_valid and (not source.is_valid or source.needs_review):
            problems = [f"{i.field}: {i.message}" for i in source.validation_issues if i.severity == "error"]
            problems += source.review_reasons
            raise ExportError(
                f"not exporting {source.source}: " + ("; ".join(dict.fromkeys(problems)) or source.status.value)
            )
        document = source.document
        if document is None:
            raise ExportError(f"{source.source} has no extracted document to export")
    else:
        document = source
        if options.require_valid:
            from ..catalog import for_model
            from ..validate import validate

            spec = for_model(type(document))
            errors = [i for i in validate(document, spec=spec) if i.severity == "error"] if spec else []
            if errors:
                raise ExportError(
                    f"not exporting {type(document).__name__}: "
                    + "; ".join(dict.fromkeys(f"{i.field}: {i.message}" for i in errors))
                )
    content = exporter(document)
    validation = None
    if options.validate_einvoice:
        if exporter.einvoice_profile is None:
            raise ExportError(f"{exporter.name} is not an e-invoice format; there are no official rules to validate it against")
        from ..einvoice.validator import EInvoiceValidationOptions, validate_einvoice

        validation = validate_einvoice(
            content.encode("utf-8"), EInvoiceValidationOptions(profile=exporter.einvoice_profile)
        )
    return ExportResult(
        format=exporter.name, media_type=exporter.media_type, content=content, einvoice_validation=validation
    )


# EU e-invoicing standards first: they are what most integrators need.
_EINVOICE = (
    ("ubl", "en16931", "en16931", "UBL 2.1 invoice / credit note, EN 16931 core"),
    ("peppol", "peppol", "peppol", "Peppol BIS Billing 3.0 (UBL 2.1)"),
    ("xrechnung-ubl", "xrechnung-ubl", "xrechnung", "XRechnung 3.0, UBL syntax (German public sector)"),
    ("xrechnung-cii", "xrechnung-cii", "factur-x-xrechnung", "XRechnung 3.0, CII syntax (= Factur-X/ZUGFeRD XRECHNUNG)"),
    ("factur-x-en16931", "factur-x-en16931", "factur-x-en16931", "Factur-X 1.0 / ZUGFeRD 2.x EN16931 (COMFORT) CII XML"),
    ("factur-x-basic", "factur-x-basic", "factur-x-basic", "Factur-X 1.0 / ZUGFeRD 2.x BASIC CII XML"),
)
for _name, _profile, _rules, _description in _EINVOICE:
    register_exporter(
        _name, partial(en16931.render, profile=_profile), accepts=(Invoice, CreditNote),
        description=_description, media_type="application/xml", einvoice_profile=_rules,
    )
register_exporter("facturae", export_to_facturae_xml, accepts=(Invoice,),
                  description="Facturae 3.2.2 (Spain)", media_type="application/xml")

__all__ = [
    "ENTRY_POINT_GROUP",
    "ExportError",
    "ExportOptions",
    "ExportResult",
    "Exporter",
    "export_document",
    "export_to_facturae_xml",
    "get_exporter",
    "list_exporters",
    "register_exporter",
]
