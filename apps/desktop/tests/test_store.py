"""Desktop release gate: only corrected and approved records reach exports."""
from __future__ import annotations

import csv
import json
import os
import zipfile

import pytest
from docket_desktop.store import DesktopStore
from PIL import Image

from docket.result import DocumentResult, DocumentStatus, SourceLocation


def _receipt(path, *, merchant: str = "Shop", total: float = 12.0) -> DocumentResult:
    return DocumentResult(
        source=str(path), document_id="test-receipt", status=DocumentStatus.SUCCEEDED,
        document_type="receipt", schema_id="receipt", schema_version="1.2",
        extracted={"merchant_name": merchant, "transaction_date": "2026-09-24",
                   "currency": "EUR", "items": [{"description": "Coffee", "price": total,
                                                   "quantity": 1, "unit_price": total}],
                   "subtotal": total, "tax_amount": 0,
                   "total_amount": total},
    )


def test_only_approved_corrected_documents_are_exported(tmp_path):
    source = tmp_path / "receipt.jpg"
    Image.new("RGB", (20, 20), "white").save(source)
    store = DesktopStore(tmp_path / "data")
    document_id, created = store.add_file(source)
    assert created
    assert store.add_file(source) == (document_id, False)
    result = _receipt(source)
    result.metrics.llm_calls = 2
    result.field_sources["merchant_name"] = SourceLocation(page=1, quote="Shop")
    result.review_reasons = ["x" * 17_000]
    store.save_result(document_id, result)
    output = tmp_path / "summary.csv"
    assert store.export_approved(output)[0] == 0
    assert store.export_approved_json(tmp_path / "approved.json") == 0
    data = json.loads(store.get(document_id)["edited_json"])
    data["merchant_name"] = "=1+1"
    store.save_edits(document_id, data)
    assert store.approve(document_id) == []
    assert store.export_approved(output)[0] == 1
    with output.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["issuer"] == "'=1+1"
    assert row["approval_status"] == "approved"
    assert row["filename"] == "receipt.jpg"
    with (tmp_path / "summary-fields.csv").open(encoding="utf-8-sig", newline="") as handle:
        fields = list(csv.DictReader(handle))
    assert next(item for item in fields if item["path"] == "/merchant_name")["value"] == "'=1+1"
    assert next(item for item in fields if item["path"] == "/items/0/description")["value"] == "Coffee"
    with (tmp_path / "summary-processing.csv").open(encoding="utf-8-sig", newline="") as handle:
        processing = list(csv.DictReader(handle))
    assert next(item for item in processing if item["path"] == "/extracted/merchant_name")["value"] == "Shop"
    assert next(item for item in processing if item["path"] == "/metrics/llm_calls")["value"] == "2"
    assert next(item for item in processing if item["path"] == "/field_sources/merchant_name/quote")["value"] == "Shop"
    long_parts = [item for item in processing if item["path"] == "/review_reasons/0"]
    assert [item["part"] for item in long_parts] == ["1", "2"]
    assert "".join(item["value"] for item in long_parts) == "x" * 17_000
    with (tmp_path / "summary-history.csv").open(encoding="utf-8-sig", newline="") as handle:
        history = list(csv.DictReader(handle))
    assert [item["action"] for item in history] == ["imported", "processed", "edited", "approved"]
    assert all(item["revision_id"] for item in history)
    assert store.export_approved_json(tmp_path / "approved.json") == 1
    payload = json.loads((tmp_path / "approved.json").read_text(encoding="utf-8"))
    assert payload["format_version"] == 2
    assert len(payload["documents"]) == 1
    assert payload["documents"][0]["document_id"] == document_id
    assert payload["documents"][0]["extracted"]["merchant_name"] == "=1+1"
    assert payload["documents"][0]["extracted"]["items"][0]["description"] == "Coffee"
    assert payload["documents"][0]["processing_result"]["metrics"]["llm_calls"] == 2
    assert payload["documents"][0]["processing_result"]["review_reasons"] == ["x" * 17_000]
    assert payload["documents"][0]["processing_result"]["extracted"]["merchant_name"] == "Shop"
    assert [event["action"] for event in payload["documents"][0]["history"]] == [
        "imported", "processed", "edited", "approved",
    ]
    assert [event["action"] for event in store.history(document_id)] == [
        "imported", "processed", "edited", "approved"
    ]
    edit_detail = json.loads(store.history(document_id)[2]["detail_json"])
    assert edit_detail["changes"] == [
        {"field": "merchant_name", "before": "Shop", "after": "=1+1"}
    ]
    assert edit_detail["actor"]
    if os.name != "nt":
        assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "data" / "documents.sqlite3").stat().st_mode & 0o777 == 0o600
        assert next((tmp_path / "data" / "files").iterdir()).stat().st_mode & 0o777 == 0o600
    backup = tmp_path / "backup.zip"
    store.backup(backup)
    with zipfile.ZipFile(backup) as archive:
        assert "documents.sqlite3" in archive.namelist()
        assert any(name.startswith("files/") for name in archive.namelist())
    store.delete(document_id)
    assert store.get(document_id) is None
    store.restore_backup(backup)
    assert store.get(document_id)["state"] == "approved"
    assert (tmp_path / "data" / "files" / f"{document_id}.jpg").is_file()
    store.close()


def test_xlsx_export_has_approved_documents_and_line_items(tmp_path):
    load_workbook = pytest.importorskip("openpyxl").load_workbook
    source = tmp_path / "receipt.jpg"
    Image.new("RGB", (20, 20), "white").save(source)
    store = DesktopStore(tmp_path / "data")
    document_id, _ = store.add_file(source)
    store.save_result(document_id, _receipt(source))
    destination = tmp_path / "approved.xlsx"
    assert store.export_approved_xlsx(destination) == 0
    data = json.loads(store.get(document_id)["edited_json"])
    data["merchant_name"] = "=1+1"
    store.save_edits(document_id, data)
    assert store.approve(document_id) == []
    assert store.export_approved_xlsx(destination) == 1
    workbook = load_workbook(destination, read_only=True, data_only=False)
    assert workbook.sheetnames == ["Documents", "Line items", "Fields", "Processing", "History"]
    documents = workbook["Documents"]
    headers = [cell.value for cell in next(documents.rows)]
    issuer = documents.cell(row=2, column=headers.index("issuer") + 1)
    assert issuer.value == "'=1+1"
    assert issuer.data_type != "f"
    assert documents.cell(row=2, column=headers.index("approval_status") + 1).value == "approved"
    assert workbook["Line items"].cell(row=2, column=5).value == "Coffee"
    assert any(row[1] == "/merchant_name" and row[4] == "'=1+1"
               for row in workbook["Fields"].iter_rows(min_row=2, values_only=True))
    assert any(row[1] == "/extracted/merchant_name" and row[4] == "Shop"
               for row in workbook["Processing"].iter_rows(min_row=2, values_only=True))
    assert [row[3] for row in workbook["History"].iter_rows(min_row=2, values_only=True)] == [
        "imported", "processed", "edited", "approved",
    ]
    workbook.close()
    store.close()


def test_invalid_correction_cannot_be_approved(tmp_path):
    source = tmp_path / "receipt.jpg"
    Image.new("RGB", (20, 20), "white").save(source)
    store = DesktopStore(tmp_path / "data")
    document_id, _ = store.add_file(source)
    store.save_result(document_id, _receipt(source))
    data = json.loads(store.get(document_id)["edited_json"])
    data["total_amount"] = 99
    store.save_edits(document_id, data)
    assert store.approve(document_id)
    assert store.get(document_id)["state"] == "review"
    invalid_backup = tmp_path / "invalid.zip"
    with zipfile.ZipFile(invalid_backup, "w") as archive:
        archive.writestr("notes.txt", "not a backup")
    with pytest.raises(ValueError, match="no document database"):
        store.restore_backup(invalid_backup)
    assert store.get(document_id)["state"] == "review"
    store.close()
