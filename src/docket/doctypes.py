"""Registry of document types: the built-in ones plus any an application adds.

A document type is a name, a Pydantic schema the LLM fills, a description the
LLM classifier reads, optional keyword rules for the free classification tier,
and optional extra validators. Registering one is all it takes for `process()`
to classify, extract, validate and return documents of that type::

    from docket import CitedDocument, register_document_type

    class DeliveryNote(CitedDocument):
        note_number: str
        supplier_name: str
        delivery_date: date

    register_document_type(
        "delivery_note",
        DeliveryNote,
        description="Delivery note / Lieferschein listing goods handed over",
        keywords=["delivery note", "lieferschein", "bon de livraison"],
    )

Validators can also be attached to built-in types, e.g. to require a PO
number on every invoice: `add_validator("invoice", my_check)`.

Separate packages can register types through the `docket.document_types`
entry point, whose target is a callable that does the registering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace as dc_replace
from importlib.metadata import entry_points
from typing import Callable, Iterable

from pydantic import BaseModel, Field

from .schemas import SCHEMA_BY_DOC_TYPE, Citation, DocType, ValidationIssue

ENTRY_POINT_GROUP = "docket.document_types"

Validator = Callable[[BaseModel, "str | None"], Iterable[ValidationIssue]]

# Weight of a plain-string keyword in the rules tier. Matches the weight the
# built-in rules give a document's own name ("invoice", "receipt"); one hit
# clears the 2-point margin over a type with no hits.
DEFAULT_KEYWORD_WEIGHT = 3.0


class DocumentTypeError(ValueError):
    """Invalid registration or unknown document type."""


class CitedDocument(BaseModel):
    """Base class for custom schemas that want source citations.

    The extraction model fills `field_locations` with the page and verbatim
    quote each value came from, and validation checks each quote really is on
    that page — the same grounding check the built-in types get.
    """

    field_locations: dict[str, Citation] = Field(
        default_factory=dict,
        description="Page and exact source region for each material extracted field.",
    )


@dataclass(frozen=True)
class DocumentType:
    name: str
    schema: type[BaseModel]
    description: str = ""
    keywords: tuple[tuple[re.Pattern, float], ...] = ()
    validators: tuple[Validator, ...] = ()
    cited_fields: tuple[str, ...] | None = None
    builtin: bool = False

    @property
    def required_citations(self) -> tuple[str, ...]:
        """Fields that must carry a source citation. Defaults to every
        required field when the schema has `field_locations`, none otherwise."""
        if self.cited_fields is not None:
            return self.cited_fields
        if "field_locations" not in self.schema.model_fields:
            return ()
        return tuple(
            name
            for name, info in self.schema.model_fields.items()
            if info.is_required() and name != "field_locations"
        )


_BUILTIN_DESCRIPTIONS = {
    DocType.INVOICE: "Invoice / factura / Rechnung requesting payment for goods or services",
    DocType.RECEIPT: "Receipt or till slip proving a payment was made",
    DocType.CONTRACT: "Contract or agreement between parties",
    DocType.PURCHASE_ORDER: "Purchase order issued by a buyer to a supplier",
    DocType.BANK_STATEMENT: "Bank account statement listing transactions",
    DocType.ACCEPTANCE_ACT: "Acceptance act / certificate of completed work or services",
    DocType.WAYBILL: "Waybill, consignment note or bill of lading for shipped goods",
    DocType.BOARDING_PASS: "Airline boarding pass",
}

_REGISTRY: dict[str, DocumentType] = {
    dt.value: DocumentType(
        name=dt.value,
        schema=schema,
        description=_BUILTIN_DESCRIPTIONS.get(dt, ""),
        builtin=True,
    )
    for dt, schema in SCHEMA_BY_DOC_TYPE.items()
}
_plugins_loaded = False


def _name_of(doc_type: DocType | str) -> str:
    return doc_type.value if isinstance(doc_type, DocType) else str(doc_type)


def _compile_keywords(
    keywords: Iterable[str | tuple[str, float]],
) -> tuple[tuple[re.Pattern, float], ...]:
    compiled = []
    for kw in keywords:
        pattern, weight = (kw, DEFAULT_KEYWORD_WEIGHT) if isinstance(kw, str) else kw
        compiled.append((re.compile(rf"\b{re.escape(pattern)}\b", re.I), float(weight)))
    return tuple(compiled)


def register_document_type(
    name: str,
    schema: type[BaseModel],
    *,
    description: str,
    keywords: Iterable[str | tuple[str, float]] = (),
    validators: Iterable[Validator] = (),
    cited_fields: Iterable[str] | None = None,
    replace: bool = False,
) -> DocumentType:
    """Teach the pipeline a new document type.

    name:         identifier returned in `classification.doc_type`, e.g. "delivery_note".
    schema:       Pydantic model the LLM fills. Subclass `CitedDocument` to get
                  page/quote citations checked against the source.
    description:  one line the LLM classifier reads to recognise the type.
    keywords:     phrases (matched case-insensitively on word boundaries) for the
                  free rules tier; a string counts 3 points, or pass (phrase, weight).
    validators:   callables `(document, raw_text) -> iterable of ValidationIssue`.
    cited_fields: fields that must carry a citation (default: every required field,
                  when the schema has `field_locations`).
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise DocumentTypeError(
            f"document type name {name!r} must be lowercase snake_case"
        )
    if name == DocType.UNKNOWN.value:
        raise DocumentTypeError("'unknown' is reserved")
    existing = _REGISTRY.get(name)
    if existing is not None and (existing.builtin or not replace):
        raise DocumentTypeError(f"document type {name!r} is already registered")
    for other in _REGISTRY.values():
        if other.schema is schema and other.name != name:
            raise DocumentTypeError(
                f"{schema.__name__} is already registered as {other.name!r}"
            )
    if not description.strip():
        raise DocumentTypeError("a description is required: the LLM classifier reads it")
    cited = tuple(cited_fields) if cited_fields is not None else None
    if cited:
        unknown = set(cited) - set(schema.model_fields)
        if unknown:
            raise DocumentTypeError(f"cited_fields not in {schema.__name__}: {sorted(unknown)}")
    doc_type = DocumentType(
        name=name,
        schema=schema,
        description=description.strip(),
        keywords=_compile_keywords(keywords),
        validators=tuple(validators),
        cited_fields=cited,
    )
    _REGISTRY[name] = doc_type
    return doc_type


def unregister_document_type(name: str) -> None:
    """Remove a custom type (built-ins cannot be removed)."""
    doc_type = _REGISTRY.get(name)
    if doc_type is None:
        raise DocumentTypeError(f"unknown document type {name!r}")
    if doc_type.builtin:
        raise DocumentTypeError(f"{name!r} is built in and cannot be removed")
    del _REGISTRY[name]


def add_validator(doc_type: DocType | str, validator: Validator) -> None:
    """Attach an extra validation rule to any type, built-in or custom."""
    name = _name_of(doc_type)
    current = get_document_type(name)
    if current is None:
        raise DocumentTypeError(f"unknown document type {name!r}")
    _REGISTRY[name] = dc_replace(current, validators=current.validators + (validator,))


def _load_plugins() -> None:
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        ep.load()()


def list_document_types() -> list[DocumentType]:
    _load_plugins()
    return list(_REGISTRY.values())


def custom_document_types() -> list[DocumentType]:
    return [t for t in list_document_types() if not t.builtin]


def get_document_type(doc_type: DocType | str) -> DocumentType | None:
    _load_plugins()
    return _REGISTRY.get(_name_of(doc_type))


def for_schema(schema: type[BaseModel]) -> DocumentType | None:
    _load_plugins()
    for doc_type in _REGISTRY.values():
        if doc_type.schema is schema:
            return doc_type
    return None


def parse_type(value: object) -> DocType | str:
    """Map a classifier's answer to a DocType, a registered custom name, or UNKNOWN."""
    text = str(value or "").strip().lower()
    try:
        return DocType(text)
    except ValueError:
        pass
    if get_document_type(text) is not None:
        return text
    return DocType.UNKNOWN


def validate_extra(document: BaseModel, raw_text: str | None) -> list[ValidationIssue]:
    """Registered validators for this document's type, plus the citation
    check for custom schemas that carry `field_locations`."""
    doc_type = for_schema(type(document))
    if doc_type is None:
        return []
    issues: list[ValidationIssue] = []
    if not doc_type.builtin and raw_text and doc_type.required_citations:
        from .validate import _check_material_locations

        issues.extend(
            _check_material_locations(document, raw_text, doc_type.required_citations)
        )
    for validator in doc_type.validators:
        issues.extend(validator(document, raw_text) or [])
    return issues


__all__ = [
    "CitedDocument",
    "DocumentType",
    "DocumentTypeError",
    "ENTRY_POINT_GROUP",
    "add_validator",
    "custom_document_types",
    "for_schema",
    "get_document_type",
    "list_document_types",
    "parse_type",
    "register_document_type",
    "unregister_document_type",
]
