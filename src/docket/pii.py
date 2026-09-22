"""Machine-readable personal-data paths for built-in and custom schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .result import DocumentResult

PiiCategory = Literal[
    "person_name",
    "address",
    "contact",
    "bank_account",
    "tax_id",
    "government_id",
    "date_of_birth",
    "travel",
]


class PiiField(BaseModel):
    path: str
    category: PiiCategory


def _walk(value: object, path: str, found: list[PiiField]) -> None:
    if isinstance(value, BaseModel):
        for name, field in value.__class__.model_fields.items():
            child = getattr(value, name)
            child_path = f"{path}.{name}" if path else name
            category = (field.json_schema_extra or {}).get("pii")
            if category and child not in (None, "", [], {}):
                if isinstance(child, list):
                    found.extend(PiiField(path=f"{child_path}[{i}]", category=category) for i in range(len(child)))
                else:
                    found.append(PiiField(path=child_path, category=category))
            _walk(child, child_path, found)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]", found)


def pii_fields(result: DocumentResult | BaseModel) -> list[PiiField]:
    """Return populated field paths carrying a declared PII category."""
    document = result.document if isinstance(result, DocumentResult) else result
    if document is None:
        return []
    found: list[PiiField] = []
    _walk(document, "", found)
    return found


__all__ = ["PiiCategory", "PiiField", "pii_fields"]
