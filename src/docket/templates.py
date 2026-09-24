r"""Vendor templates: deterministic extraction for known document layouts.

A template names an issuer and how that issuer's documents print their
fields — the label a value sits next to, the column each line-item number
lives in. A document that matches a template is read by rules over the
acquired text and layout: no LLM call, no model variance, no fabricated
row to balance a total. Every read value still carries a citation (the
line it was read from), so grounding, location and review behave exactly
as they do for model extraction — a template read is checked, not trusted.

The pipeline uses a template only when it produces an instance that
validates clean; otherwise it falls back to the model and the template
match is recorded nowhere. Templates are per-vendor knowledge, not a
general extractor: one that matches too greedily (an issuer pattern like
"Invoice") will misfire, so issuer patterns must name the vendor.

    from docket.templates import VendorTemplate, FieldRule, ItemsRule, register_vendor_template

    register_vendor_template(VendorTemplate(
        template_id="acme-invoice",
        schema_id="invoice",
        issuer=["ACME Supplies"],
        fields=(
            FieldRule(field="invoice_number", label="Invoice No", value=r"Invoice No:?\s*([A-Z0-9-]+)"),
            FieldRule(field="total_amount", label="Total", value=r"([\d.,]+)"),
        ),
        items=ItemsRule(columns={"description": 0, "quantity": 1, "unit_price": 2, "total": 3}),
    ))

A `FieldRule` finds the first page line matching `label` (case-insensitive;
no label = any line), then the first line from there through `lines_after`
following lines where `value` — a regex with one capture group — matches.
The captured string is coerced to the schema field's type: amounts with
`docket.amounts.parse_amount`, dates under the document's own day/month
convention, everything else as text (`map` replaces literals first, e.g.
"€" -> "EUR"; `strip` removes inner whitespace, e.g. printed IBAN groups).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, Field

from .amounts import parse_amount
from .catalog import SchemaSpec
from .layout.models import DocumentLayout

# ---- the template model ----------------------------------------------------------------------


class FieldRule(BaseModel):
    """One schema field read from the page text."""

    field: str = Field(description="Schema path, e.g. 'invoice_number' or 'seller.tax_ids[0].value'.")
    label: str = Field(default="", description="Regex the line must match first (case-insensitive); empty = any line.")
    value: str = Field(description="Regex with one capture group, applied to the label line (and `lines_after` more).")
    page: int | None = Field(default=None, ge=1, description="Restrict to one page; default: every page in order.")
    lines_after: int = Field(default=0, ge=0, description="Also try this many lines below the label line.")
    strip: bool = Field(default=False, description="Remove all whitespace from the captured value (IBAN groups).")
    map: dict[str, str] = Field(default_factory=dict, description="Literal replacements before coercion ('€' -> 'EUR').")


class ItemsRule(BaseModel):
    """Line items read from a detected table by column position."""

    page: int = Field(default=1, ge=1)
    table: int = Field(default=0, ge=0, description="Index into the page's detected tables.")
    header: bool = Field(default=True, description="Skip the first grid row (column titles).")
    columns: dict[str, int] = Field(description="Line-item attribute -> column index, e.g. {'description': 0, 'total': 3}.")


TEXT_CELL_ARTIFACTS = " |[]"  # OCR reads ruled lines as stray '|' and '[' words


class VendorTemplate(BaseModel):
    template_id: str
    schema_id: str
    description: str = ""
    builtin: bool = Field(default=False, description="Shipped by Docket rather than registered by the application.")
    issuer: tuple[str, ...] = Field(description="Regexes; ALL must appear in the document for a match.")
    fields: tuple[FieldRule, ...] = ()
    items: ItemsRule | None = None


# ---- registry ---------------------------------------------------------------------------------


_REGISTRY: dict[str, VendorTemplate] = {}


def register_vendor_template(template: VendorTemplate, *, builtin: bool = False) -> VendorTemplate:
    if not template.issuer:
        raise ValueError("a vendor template needs at least one issuer pattern — 'invoice' matches everything")
    if template.template_id in _REGISTRY:
        raise ValueError(f"vendor template {template.template_id!r} is already registered")
    _REGISTRY[template.template_id] = template.model_copy(
        update={"description": template.description or template.template_id, "builtin": builtin}
    )
    return _REGISTRY[template.template_id]


def unregister_vendor_template(template_id: str) -> None:
    _REGISTRY.pop(template_id, None)


def get_vendor_template(template_id: str) -> VendorTemplate | None:
    return _REGISTRY.get(template_id)


def list_vendor_templates() -> list[VendorTemplate]:
    return list(_REGISTRY.values())


def _fold(text: str) -> str:
    """OCR routinely drops diacritics ('Bürobedarf' -> 'Burobedarf'), so
    issuer and label matching runs on both sides of this fold; value regexes
    stay raw — the captured string must be what the page printed."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def match_vendor_template(text: str, schema_id: str) -> VendorTemplate | None:
    """The first template of this schema whose every issuer pattern appears."""
    folded = _fold(text)
    for template in _REGISTRY.values():
        if template.schema_id != schema_id:
            continue
        if all(re.search(_fold(pattern), folded) for pattern in template.issuer):
            return template
    return None


# ---- extraction -------------------------------------------------------------------------------


def extract_with_template(
    layout: DocumentLayout, spec: SchemaSpec, template: VendorTemplate
) -> BaseModel | None:
    """Build the schema instance from the template's rules, with a citation
    per read value, or None when any rule fails — the pipeline then falls
    back to the model, so a rule that doesn't hold costs an LLM call, not a
    wrong answer."""
    convention = _convention(layout.text)
    raw: dict = {}
    citations: dict[str, dict] = {}
    for rule in template.fields:
        found = _find_rule(layout, rule)
        if found is None:
            return None
        page, quote, captured = found
        _set_path(raw, rule.field, _coerce(spec.model, rule.field, captured, convention, rule))
        citations[rule.field] = {"page": page, "quote": quote}

    if template.items is not None and spec.line_items is not None:
        rows, row_citations = _read_items(layout, template.items, spec)
        if rows:
            raw[spec.line_items.path] = rows
            citations.update(row_citations)

    raw["field_locations"] = citations
    try:
        return spec.model.model_validate(raw)
    except Exception:  # noqa: BLE001 — any rule miss is a fallback, not a crash
        return None


def _find_rule(layout: DocumentLayout, rule: FieldRule) -> tuple[int, str, str] | None:
    label = re.compile(_fold(rule.label)) if rule.label else None
    value = re.compile(rule.value)
    pages = [p for p in layout.pages if rule.page in (None, p.page_number)]
    for page in pages:
        lines = page.lines
        folded = [ _fold(line.text) for line in lines ]
        for i in range(len(lines)):
            if label is not None and not label.search(folded[i]):
                continue
            # lines_after=0: the value sits on the label line; lines_after=n:
            # only the n lines below — declaring it says the label line has none.
            first = i if rule.lines_after == 0 else i + 1
            for j in range(first, min(len(lines), i + 1 + rule.lines_after)):
                match = value.search(lines[j].text)
                if match and match.group(1):
                    captured = match.group(1).strip()
                    if rule.map:
                        for old, new in rule.map.items():
                            captured = captured.replace(old, new)
                    if rule.strip:
                        captured = "".join(captured.split())
                    return page.page_number, lines[i].text, captured
    return None


def _read_items(layout: DocumentLayout, rule: ItemsRule, spec: SchemaSpec):
    assert spec.line_items is not None  # an items rule is only accepted for schemas with rows
    page = layout.page(rule.page)
    if page is None or rule.table >= len(page.tables):
        return [], {}
    grid = page.tables[rule.table].grid()[1 if rule.header else 0 :]
    rows: list[dict] = []
    citations: dict[str, dict] = {}
    for row in grid:
        item: dict = {}
        quote = " ".join(cell for cell in row)
        for attr, column in rule.columns.items():
            if column >= len(row):
                item = {}
                break
            raw_cell = row[column]
            coerced = _coerce_item_attr(spec, attr, raw_cell)
            if coerced is None:
                item = {}
                break
            if isinstance(coerced, str):
                coerced = coerced.strip(TEXT_CELL_ARTIFACTS).strip()
            item[attr] = coerced
        if not item or "description" not in item:
            continue  # a row that didn't parse whole is skipped; arithmetic then flags it honestly
        index = len(rows)
        for attr in item:
            citations[f"{spec.line_items.path}[{index}].{attr}"] = {
                "page": rule.page,
                "quote": quote,
            }
        rows.append(item)
    return rows, citations


def _coerce_item_attr(spec: SchemaSpec, attr: str, raw: str):
    assert spec.line_items is not None
    item_model = _annotation_of(spec.model, spec.line_items.path)
    if isinstance(raw, str) and _kind_of(item_model, attr) == "number":
        return parse_amount(raw)
    return raw


def _convention(text: str) -> str | None:
    from .validate import _document_date_convention

    return _document_date_convention(text)


def _annotation_of(model: type[BaseModel], path: str):
    """The model class or field annotation a dotted path points into."""
    current: Any = model
    for part in path.split("."):
        name, _, index = part.partition("[")
        annotation = current.model_fields[name].annotation
        if get_origin(annotation) is list:
            annotation = get_args(annotation)[0]
        if index:
            current = annotation
            continue
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            current = annotation
        else:
            return annotation
    return current


def _kind_of(model_or_annotation: object, name: str) -> str:
    """'date', 'number' or 'text' — how to coerce a captured string."""
    annotation: Any
    if isinstance(model_or_annotation, type) and issubclass(model_or_annotation, BaseModel):
        annotation = model_or_annotation.model_fields[name].annotation
    else:
        annotation = model_or_annotation
    if annotation is date:
        return "date"
    if annotation in (float, int):
        return "number"
    if get_origin(annotation) in (Union, __import__("types").UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if args and args[0] is date:
            return "date"
        if args and args[0] in (float, int):
            return "number"
    return "text"


def _coerce(model: type[BaseModel], path: str, captured: str, convention: str | None, rule: FieldRule):
    target = _annotation_of(model, path)
    kind = _kind_of(target, path.rsplit(".", 1)[-1].partition("[")[0])
    if kind == "date":
        parsed = _parse_date(captured, convention)
        return parsed if parsed else captured
    if kind == "number":
        return parse_amount(captured) if parse_amount(captured) is not None else captured
    return captured


def _parse_date(raw: str, convention: str | None) -> date | None:
    from datetime import datetime

    raw = raw.strip()
    orders = ("%d/%m/%Y", "%d-%m-%Y") if convention != "mdy" else ("%m/%d/%Y", "%m-%d-%Y")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", *orders, "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _set_path(data: dict, path: str, value) -> None:
    """Set a dotted path with optional list indices: 'seller.tax_ids[0].value'."""
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        name, _, index = part.partition("[")
        if index:
            position = int(index.rstrip("]"))
            bucket = current.setdefault(name, [])
            while len(bucket) <= position:
                bucket.append({})
            current = bucket[position]
        else:
            current = current.setdefault(name, {})
    leaf = parts[-1]
    name, _, index = leaf.partition("[")
    if index:
        position = int(index.rstrip("]"))
        bucket = current.setdefault(name, [])
        while len(bucket) <= position:
            bucket.append({})
        bucket[position] = value
    else:
        current[name] = value


# ---- built-in examples ------------------------------------------------------------------------
# Two golden-corpus vendors, fictional, as working examples and as the
# integration the tests run end to end. Real vendors belong to user code.

BUILTIN_TEMPLATES: tuple[VendorTemplate, ...] = (
    VendorTemplate(
        template_id="nordlicht-buerobedarf-invoice",
        schema_id="invoice",
        description="Nordlicht Bürobedarf GmbH — German invoice (golden corpus vendor)",
        issuer=("Nordlicht Bürobedarf",),
        fields=(
            FieldRule(field="invoice_number", label="Rechnungsnummer", value=r"Rechnungsnummer:?\s*(\S+)"),
            FieldRule(field="issue_date", label="Rechnungsdatum", value=r"Rechnungsdatum:?\s*([\d.]+)"),
            FieldRule(field="due_date", label="Fällig", value=r":\s*([\d.]+)"),
            FieldRule(field="seller.name", value=r"(Nordlicht B[uü]robedarf GmbH)", map={"Burobedarf": "Bürobedarf"}),
            FieldRule(field="seller.tax_ids[0].value", label="USt-IdNr", value=r"USt-?IdNr\.?:?\s*([A-Z0-9]+)"),
            FieldRule(field="buyer.name", label="Rechnungsempfänger", value=r"^(.+?)\s*$", lines_after=1),
            FieldRule(field="payment_account.iban", label="IBAN", value=r"IBAN:?\s*([A-Z0-9 ]+?)\s*(?:BIC|$)", strip=True),
            FieldRule(field="currency", label="Gesamtbetrag", value=r"\b(EUR|USD|CHF|GBP)\b"),
            FieldRule(field="subtotal", label="Zwischensumme", value=r"Zwischensumme:?\s*([\d.,]+)"),
            FieldRule(field="tax_amount", label=r"USt\.\s*\d", value=r"%:?\s*([\d.,]+)"),
            FieldRule(field="total_amount", label="Gesamtbetrag", value=r"Gesamtbetrag:?\s*([\d.,]+)"),
        ),
        items=ItemsRule(header=False, columns={"description": 1, "quantity": 2, "unit_price": 3, "total": 4}),
    ),
    VendorTemplate(
        template_id="distribuciones-albufera-invoice",
        schema_id="invoice",
        description="Distribuciones Albufera S.L. — Spanish invoice (golden corpus vendor)",
        issuer=("Distribuciones Albufera",),
        fields=(
            FieldRule(field="invoice_number", label="Factura n", value=r"Factura n\.[º®o]?:?\s*(\S+)"),
            FieldRule(field="issue_date", label="Fecha", value=r"Fecha:?\s*([\d/]+)"),
            FieldRule(field="due_date", label="Vencimiento", value=r"Vencimiento:?\s*([\d/]+)"),
            FieldRule(field="seller.name", value=r"(Distribuciones Albufera S\.L\.)"),
            FieldRule(field="seller.tax_ids[0].value", label="CIF", value=r"CIF:?\s*([A-Z0-9]+)"),
            FieldRule(field="buyer.name", label="Cliente", value=r"^(.+?)\s*$", lines_after=1),
            FieldRule(field="currency", label="Total factura", value=r"(€)", map={"€": "EUR"}),
            FieldRule(field="subtotal", label="Base imponible", value=r"Base imponible:?\s*([\d.,]+)"),
            FieldRule(field="tax_amount", label=r"IVA\s+\d+", value=r"%:?\s*([\d.,]+)"),
            FieldRule(field="total_amount", label="Total factura", value=r"Total factura:?\s*([\d.,]+)"),
        ),
        items=ItemsRule(columns={"description": 0, "quantity": 1, "unit_price": 2, "total": 3}),
    ),
)

for _template in BUILTIN_TEMPLATES:
    register_vendor_template(_template, builtin=True)
