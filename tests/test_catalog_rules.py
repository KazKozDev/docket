"""Business rules of the schemas added with the catalog: each rule fires on
the fault it exists for and stays silent on the clean fixture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docket.catalog import get_schema
from docket.validate import validate

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"


def _issues(schema_id: str, **changes):
    spec = get_schema(schema_id)
    text = (FIXTURES / f"{schema_id}.txt").read_text()
    data = json.loads((FIXTURES / f"{schema_id}.expected.json").read_text())
    data.update(changes)
    return validate(spec.model.model_validate(data), pages=[text], spec=spec)


def _fields(issues, severity="error"):
    return {i.field for i in issues if i.severity == severity}


# ---- credit note --------------------------------------------------


def test_credit_note_without_an_original_invoice_is_a_warning():
    issues = _issues("credit_note", references=[])
    assert "references" in _fields(issues, "warning")
    assert "references" not in _fields(issues)


def test_credit_note_signs_are_notation():
    spec = get_schema("credit_note")
    data = json.loads((FIXTURES / "credit_note.expected.json").read_text())
    data.update(subtotal=-300.0, tax_amount=-57.0, total_amount=-357.0)
    note = spec.model.model_validate(data)
    assert (note.subtotal, note.tax_amount, note.total_amount) == (300.0, 57.0, 357.0)


# ---- delivery note ----------------------------------------------------------------


def test_delivery_note_over_delivery_is_a_warning():
    items = [{"description": "Eichenbretter 30 mm", "quantity_ordered": 40, "quantity_delivered": 45}]
    issues = _issues("delivery_note", items=items)
    assert "items[0].quantity_delivered" in _fields(issues, "warning")


def test_delivery_note_supplier_and_recipient_must_differ():
    issues = _issues("delivery_note", recipient={"name": "Holzwerk Bayern GmbH"})
    assert "recipient.name" in _fields(issues)
