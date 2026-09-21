"""Accounting and e-Invoicing export module for Docket.

Provides converters for electronic invoicing standards (UBL 2.1 / Peppol BIS,
Facturae 3.2.2, ZUGFeRD 2.2 / Factur-X / XRechnung) and ERP systems (SAP,
QuickBooks, Xero, 1C).

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

from ..schemas import AcceptanceAct, BankStatement, Invoice, Receipt
from .einvoice import (
    export_to_facturae_xml,
    export_to_ubl_xml,
    export_to_zugferd_xml,
)
from .erp import (
    export_to_1c_client_bank,
    export_to_1c_enterprise_xml,
    export_to_quickbooks_iif,
    export_to_quickbooks_json,
    export_to_sap_idoc,
    export_to_sap_journal_csv,
    export_to_xero_csv,
    export_to_xero_json,
)

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

    def __call__(self, document: BaseModel) -> str:
        if not isinstance(document, self.accepts):
            wanted = " or ".join(t.__name__ for t in self.accepts)
            raise ExportError(
                f"{self.name} export requires {wanted}, got {type(document).__name__}"
            )
        output = self.func(document)
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
    replace: bool = False,
) -> Exporter:
    """Make `func` available as export format `name`.

    `func` takes one document and returns a string, or a JSON-serializable
    object that is rendered as JSON (media type application/json by default).
    """
    if name in _REGISTRY and not replace:
        raise ExportError(f"exporter {name!r} is already registered")
    exporter = Exporter(name, func, tuple(accepts), description, media_type or "text/plain")
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
            "needs review. Never emit a binding e-invoice from unchecked data."
        ),
    )


class ExportResult(BaseModel):
    format: str
    media_type: str
    content: str


def export_document(
    source: "DocumentResult | BaseModel", format: str, options: ExportOptions | None = None
) -> ExportResult:
    """Render a processed document in the named format.

    `source` is a DocumentResult (its typed document is exported) or a schema
    instance you built yourself (exported as is, no validity check).
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
    return ExportResult(format=exporter.name, media_type=exporter.media_type, content=exporter(document))


# EU e-invoicing standards first: they are what most integrators need.
register_exporter("ubl", export_to_ubl_xml, accepts=(Invoice,),
                  description="UBL 2.1 invoice (Peppol BIS Billing 3.0 compatible)", media_type="application/xml")
register_exporter("zugferd", partial(export_to_zugferd_xml, profile="EN16931"),
                  accepts=(Invoice,), description="ZUGFeRD 2.2 / Factur-X CII, EN 16931 profile", media_type="application/xml")
register_exporter("xrechnung", partial(export_to_zugferd_xml, profile="XRECHNUNG"),
                  accepts=(Invoice,), description="XRechnung CII (German public sector)", media_type="application/xml")
register_exporter("facturae", export_to_facturae_xml, accepts=(Invoice,),
                  description="Facturae 3.2.2 (Spain)", media_type="application/xml")
register_exporter("sap-idoc", export_to_sap_idoc, accepts=(Invoice,),
                  description="SAP INVOIC IDoc XML", media_type="application/xml")
register_exporter("sap-csv", export_to_sap_journal_csv, accepts=(Invoice, BankStatement),
                  description="SAP journal entry CSV", media_type="text/csv")
register_exporter("xero-csv", export_to_xero_csv, accepts=(Invoice, Receipt),
                  description="Xero bills import CSV", media_type="text/csv")
register_exporter("xero-json", export_to_xero_json, accepts=(Invoice, Receipt),
                  description="Xero API invoice JSON", media_type="application/json")
register_exporter("quickbooks-iif", export_to_quickbooks_iif, accepts=(Invoice, Receipt),
                  description="QuickBooks Desktop IIF", media_type="text/plain")
register_exporter("quickbooks-json", export_to_quickbooks_json, accepts=(Invoice, Receipt),
                  description="QuickBooks Online API bill JSON", media_type="application/json")
register_exporter("1c-bank", export_to_1c_client_bank, accepts=(BankStatement,),
                  description="1C Client-Bank exchange file", media_type="text/plain")
register_exporter("1c-enterprise", export_to_1c_enterprise_xml,
                  accepts=(Invoice, AcceptanceAct), description="1C:Enterprise XML", media_type="application/xml")

__all__ = [
    "ENTRY_POINT_GROUP",
    "ExportError",
    "ExportOptions",
    "ExportResult",
    "Exporter",
    "export_document",
    "export_to_1c_client_bank",
    "export_to_1c_enterprise_xml",
    "export_to_facturae_xml",
    "export_to_quickbooks_iif",
    "export_to_quickbooks_json",
    "export_to_sap_idoc",
    "export_to_sap_journal_csv",
    "export_to_ubl_xml",
    "export_to_xero_csv",
    "export_to_xero_json",
    "export_to_zugferd_xml",
    "get_exporter",
    "list_exporters",
    "register_exporter",
]
