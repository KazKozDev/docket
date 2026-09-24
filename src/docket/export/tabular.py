"""Batch results as tables: one summary CSV, one line-item CSV, JSON Lines.

The summary CSV has the same columns for every document type, so one file
can hold a mixed batch:

    source, document_id, document_type, schema_id, schema_version, status,
    needs_review, review_reasons, validation_error_count,
    validation_warning_count, error_code, error_message,
    document_number, document_date, issuer, recipient, currency, subtotal,
    tax_amount, total_amount

The last eight come from each schema's `summary` map (e.g. an invoice's
`seller.name` is `issuer`); a schema without that field leaves it empty.
`review_reasons` are joined with " | ".

Line items go to a second CSV, one row per item, linked by `document_id`:

    document_id, source, schema_id, line_no, description, sku, quantity,
    unit_of_measure, unit_price, total, tax_rate_percent

Which rows count as items is the schema's `line_items` map: invoice lines,
receipt items, bank statement transactions, utility charges, ...
"""
from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Iterator
from typing import IO, cast

from ..catalog.registry import (
    LINE_ITEM_COLUMNS,
    SUMMARY_COLUMNS,
    SchemaSpec,
    get_schema,
)
from ..result import DocumentResult

RESULT_COLUMNS: tuple[str, ...] = (
    "source",
    "document_id",
    "document_type",
    "schema_id",
    "schema_version",
    "status",
    "needs_review",
    "review_reasons",
    "validation_error_count",
    "validation_warning_count",
    "error_code",
    "error_message",
) + SUMMARY_COLUMNS

ITEM_COLUMNS: tuple[str, ...] = ("document_id", "source", "schema_id", "line_no") + LINE_ITEM_COLUMNS


def _at(data: object, path: str) -> object:
    current = data
    for part in path.split("."):
        name, _, index = part.partition("[")
        if not isinstance(current, dict):
            return None
        current = current.get(name)
        if index:
            try:
                current = current[int(index.rstrip("]"))]  # type: ignore[index]
            except (IndexError, TypeError, ValueError):
                return None
    return current


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        # A structured value where a scalar column was mapped: keep it
        # readable rather than dumping JSON into the cell.
        if isinstance(value, dict) and "name" in value:
            return value["name"]
        return json.dumps(value, ensure_ascii=False)
    return value


def _spec(result: DocumentResult) -> SchemaSpec | None:
    if result.schema_id is None:
        return None
    return get_schema(result.schema_id, result.schema_version) or get_schema(result.schema_id)


def result_row(result: DocumentResult) -> dict[str, object]:
    errors = sum(1 for i in result.validation_issues if i.severity == "error")
    row: dict[str, object] = {
        "source": result.source,
        "document_id": result.document_id,
        "document_type": result.document_type,
        "schema_id": result.schema_id,
        "schema_version": result.schema_version,
        "status": result.status.value,
        "needs_review": result.needs_review,
        "review_reasons": " | ".join(result.review_reasons),
        "validation_error_count": errors,
        "validation_warning_count": len(result.validation_issues) - errors,
        "error_code": result.error.code if result.error else None,
        "error_message": result.error.message if result.error else None,
    }
    spec = _spec(result)
    for column in SUMMARY_COLUMNS:
        path = spec.summary.get(column) if spec else None
        row[column] = _at(result.extracted, path) if path and result.extracted else None
    return {k: _cell(v) for k, v in row.items()}


def line_item_rows(result: DocumentResult) -> Iterator[dict[str, object]]:
    spec = _spec(result)
    if spec is None or spec.line_items is None or not result.extracted:
        return
    items = cast(list, _at(result.extracted, spec.line_items.path) or [])
    for n, item in enumerate(items, start=1):
        row: dict[str, object] = {
            "document_id": result.document_id,
            "source": result.source,
            "schema_id": result.schema_id,
            "line_no": n,
        }
        for column in LINE_ITEM_COLUMNS:
            field = spec.line_items.columns.get(column)
            row[column] = _at(item, field) if field else None
        yield {k: _cell(v) for k, v in row.items()}


class CsvWriter:
    """Streams rows as results arrive; the header is written once."""

    def __init__(self, stream: IO[str], columns: tuple[str, ...]):
        self._writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        self._writer.writeheader()

    def write(self, row: dict[str, object]) -> None:
        self._writer.writerow(row)


def write_results_csv(results: Iterable[DocumentResult], stream: IO[str]) -> None:
    writer = CsvWriter(stream, RESULT_COLUMNS)
    for result in results:
        writer.write(result_row(result))


def write_line_items_csv(results: Iterable[DocumentResult], stream: IO[str]) -> None:
    writer = CsvWriter(stream, ITEM_COLUMNS)
    for result in results:
        for row in line_item_rows(result):
            writer.write(row)


def jsonl_line(result: DocumentResult, *, include_layout: bool = True) -> str:
    exclude = None if include_layout else {"layout": True}
    return result.model_dump_json(exclude=exclude) + "\n"


def write_jsonl(results: Iterable[DocumentResult], stream: IO[str], *, include_layout: bool = True) -> None:
    for result in results:
        stream.write(jsonl_line(result, include_layout=include_layout))


__all__ = [
    "ITEM_COLUMNS",
    "RESULT_COLUMNS",
    "CsvWriter",
    "jsonl_line",
    "line_item_rows",
    "result_row",
    "write_jsonl",
    "write_line_items_csv",
    "write_results_csv",
]
