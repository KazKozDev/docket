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
from typing import Any, Callable

from pydantic import BaseModel

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

ENTRY_POINT_GROUP = "docket.exporters"


class ExportError(ValueError):
    """The requested format is unknown or cannot represent this document."""


@dataclass(frozen=True)
class Exporter:
    name: str
    func: Callable[[Any], Any]
    accepts: tuple[type[BaseModel], ...]
    description: str = ""

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
    replace: bool = False,
) -> Exporter:
    """Make `func` available as export format `name`.

    `func` takes one document and returns a string, or a JSON-serializable
    object that is rendered as JSON.
    """
    if name in _REGISTRY and not replace:
        raise ExportError(f"exporter {name!r} is already registered")
    exporter = Exporter(name, func, tuple(accepts), description)
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


def export_document(document: BaseModel, fmt: str) -> str:
    """Render `document` in the named format, e.g. `export_document(invoice, "xrechnung")`."""
    return get_exporter(fmt)(document)


# EU e-invoicing standards first: they are what most integrators need.
register_exporter("ubl", export_to_ubl_xml, accepts=(Invoice,),
                  description="UBL 2.1 invoice (Peppol BIS Billing 3.0 compatible)")
register_exporter("zugferd", partial(export_to_zugferd_xml, profile="EN16931"),
                  accepts=(Invoice,), description="ZUGFeRD 2.2 / Factur-X CII, EN 16931 profile")
register_exporter("xrechnung", partial(export_to_zugferd_xml, profile="XRECHNUNG"),
                  accepts=(Invoice,), description="XRechnung CII (German public sector)")
register_exporter("facturae", export_to_facturae_xml, accepts=(Invoice,),
                  description="Facturae 3.2.2 (Spain)")
register_exporter("sap-idoc", export_to_sap_idoc, accepts=(Invoice,),
                  description="SAP INVOIC IDoc XML")
register_exporter("sap-csv", export_to_sap_journal_csv, accepts=(Invoice, BankStatement),
                  description="SAP journal entry CSV")
register_exporter("xero-csv", export_to_xero_csv, accepts=(Invoice, Receipt),
                  description="Xero bills import CSV")
register_exporter("xero-json", export_to_xero_json, accepts=(Invoice, Receipt),
                  description="Xero API invoice JSON")
register_exporter("quickbooks-iif", export_to_quickbooks_iif, accepts=(Invoice, Receipt),
                  description="QuickBooks Desktop IIF")
register_exporter("quickbooks-json", export_to_quickbooks_json, accepts=(Invoice, Receipt),
                  description="QuickBooks Online API bill JSON")
register_exporter("1c-bank", export_to_1c_client_bank, accepts=(BankStatement,),
                  description="1C Client-Bank exchange file")
register_exporter("1c-enterprise", export_to_1c_enterprise_xml,
                  accepts=(Invoice, AcceptanceAct), description="1C:Enterprise XML")

__all__ = [
    "ENTRY_POINT_GROUP",
    "ExportError",
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
