"""Exercise the native review flow without opening a display or using an LLM."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from docket_desktop.store import DesktopStore
from docket_desktop.ui import (
    DesktopWindow,
    SettingsDialog,
    _suggested_save_path,
)
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QWidget,
)

from docket.result import DocumentResult, DocumentStatus


def test_import_process_edit_approve_in_native_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "receipt.png"
    Image.new("RGB", (60, 60), "white").save(source)
    store = DesktopStore(tmp_path / "data")
    window = DesktopWindow(store)
    window._import([source])
    assert window.current_id
    assert window.documents.count() == 1
    assert window.delete_button.isEnabled()
    assert window.delete_button.text() == ""
    assert not window.delete_button.icon().isNull()
    assert window.delete_button.accessibleName() == "Delete selected document"
    assert window.page_label.text() == "Page 1 / 1"

    result = DocumentResult(
        source=str(source), document_id=window.current_id,
        status=DocumentStatus.SUCCEEDED, document_type="receipt",
        schema_id="receipt", schema_version="1.2",
        extracted={"merchant_name": "Shop", "transaction_date": "2026-09-24",
                   "currency": "EUR", "items": [], "subtotal": 12.0,
                   "tax_amount": 0.0, "total_amount": 12.0},
    )
    monkeypatch.setattr("docket_desktop.ui.process_document", lambda path, options: result)
    window.process_selected()
    deadline = time.monotonic() + 3
    while window._thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._thread is None
    assert store.get(window.current_id)["state"] == "review"
    merchant = next(window.fields.topLevelItem(i) for i in range(window.fields.topLevelItemCount())
                    if window.fields.topLevelItem(i).text(0) == "merchant_name")
    merchant.setText(1, "Corrected shop")
    assert window.save_edits()
    window.approve()
    assert store.get(window.current_id)["state"] == "approved"
    assert json.loads(store.get(window.current_id)["edited_json"])["merchant_name"] == "Corrected shop"
    assert window.fields.topLevelItemCount() > 0
    dialog_defaults = []

    def choose_export(_parent, _title, suggested_path, _filter):
        dialog_defaults.append(suggested_path)
        return str(tmp_path / Path(suggested_path).name), _filter

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_export)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    export_actions = window.export_button.menu().actions()
    assert [action.text() for action in export_actions] == [
        "Excel (.xlsx)", "CSV (.csv)", "JSON (.json)",
    ]
    for action in export_actions:
        action.trigger()
    assert [Path(path).suffix for path in dialog_defaults] == [".xlsx", ".csv", ".json"]
    assert Path(dialog_defaults[0]).is_absolute()
    assert Path(dialog_defaults[0]).parent == (
        Path.home() / "Documents" if (Path.home() / "Documents").is_dir() else Path.home()
    )
    assert (tmp_path / "approved-documents.csv").exists()
    assert (tmp_path / "approved-documents-items.csv").exists()
    assert (tmp_path / "approved-documents-fields.csv").exists()
    assert (tmp_path / "approved-documents-processing.csv").exists()
    assert (tmp_path / "approved-documents-history.csv").exists()
    assert (tmp_path / "approved-documents.xlsx").exists()
    assert (tmp_path / "approved-documents.json").exists()
    window.close()
    del app


def test_vlm_button_uses_vision_ocr_without_changing_default_engine(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "invoice.png"
    Image.new("RGB", (60, 60), "white").save(source)
    window = DesktopWindow(DesktopStore(tmp_path / "data"))
    window._import([source])
    window.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window.settings.setValue("backend", "tesseract")
    seen_options = []
    result = DocumentResult(
        source=str(source), document_id=window.current_id,
        status=DocumentStatus.SUCCEEDED, document_type="invoice",
        schema_id="invoice", schema_version="1.2", extracted={},
    )

    def fake_process(_path, options):
        seen_options.append(options)
        return result

    monkeypatch.setattr("docket_desktop.ui.process_document", fake_process)
    window.vlm_button.click()
    deadline = time.monotonic() + 3
    while window._thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._thread is None
    assert len(seen_options) == 1
    assert seen_options[0].ocr.backend == "vlm"
    assert seen_options[0].ocr.fallbacks == []
    assert seen_options[0].ocr.use_pdf_text is False
    assert window.settings.value("backend") == "tesseract"
    assert window.store.get(window.current_id)["state"] == "review"
    window.close()
    del app


def test_save_path_falls_back_to_home_if_documents_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _suggested_save_path("docket-backup.zip") == str(tmp_path / "docket-backup.zip")
    (tmp_path / "Documents").mkdir()
    assert _suggested_save_path("approved-documents.csv") == str(
        tmp_path / "Documents" / "approved-documents.csv"
    )


def test_zoom_enlarges_small_scan_and_allows_scrolling(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "small-receipt.png"
    Image.new("RGB", (120, 200), "white").save(source)
    window = DesktopWindow(DesktopStore(tmp_path / "data"))
    window.show()
    app.processEvents()
    window._import([source])
    app.processEvents()
    initial_width = window.preview.pixmap().width()
    window.zoom_in()
    app.processEvents()
    assert window.zoom_label.text() == "125%"
    assert window.preview.pixmap().width() > initial_width
    assert window._preview_scroll.horizontalScrollBar().maximum() > 0
    window.close()
    del app


def test_initial_preview_fits_after_window_is_shown(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "receipt.png"
    Image.new("RGB", (500, 700), "white").save(source)
    store = DesktopStore(tmp_path / "data")
    store.add_file(source)
    window = DesktopWindow(store)
    window.show()
    app.processEvents()
    assert window.preview.pixmap().width() <= window._preview_scroll.viewport().width()
    window.close()
    del app


def test_settings_show_only_the_selected_provider_fields(tmp_path):
    app = QApplication.instance() or QApplication([])
    old_palette = app.palette()
    dark_palette = QPalette(old_palette)
    dark_palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    app.setPalette(dark_palette)
    store = DesktopStore(tmp_path / "data")
    window = DesktopWindow(store)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    dialog = SettingsDialog(window, settings)
    dialog.show()
    app.processEvents()
    assert dialog.ollama_host.isVisible()
    assert not dialog.api_key.isVisible()
    assert dialog.text_model.width() > 300
    label = dialog._provider_form.labelForField(dialog.text_model)
    assert label is not None
    assert label.palette().color(QPalette.ColorRole.WindowText).name() == "#20313a"
    dialog.provider.setCurrentText("openai")
    app.processEvents()
    assert dialog.api_key.isVisible()
    assert dialog.base_url.isVisible()
    assert not dialog.ollama_host.isVisible()
    dialog.close()
    window.close()
    app.setPalette(old_palette)


def test_add_menu_imports_files_and_folder(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = DesktopWindow(DesktopStore(tmp_path / "data"))
    file_source = tmp_path / "invoice.png"
    folder = tmp_path / "receipts"
    folder.mkdir()
    folder_source = folder / "receipt.png"
    Image.new("RGB", (60, 60), "white").save(file_source)
    Image.new("RGB", (60, 60), "black").save(folder_source)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *_args: ([str(file_source)], ""))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(folder))
    assert [action.text() for action in window.add_button.menu().actions()] == ["Files…", "Folder…"]
    window.add_button.menu().actions()[0].trigger()
    window.add_button.menu().actions()[1].trigger()
    assert window.documents.count() == 2
    assert window.findChildren(QWidget, "toolbarBrand")
    assert not any(button.text() in {"Add folder", "Process all new", "More"}
                   for button in window.findChildren(QPushButton))
    window.close()
    del app


def test_settings_contains_backup_actions(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = DesktopWindow(DesktopStore(tmp_path / "data"))
    called = []
    monkeypatch.setattr(window, "backup", lambda owner: called.append(("backup", owner)))
    monkeypatch.setattr(window, "restore", lambda owner: called.append(("restore", owner)))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    dialog = SettingsDialog(window, settings)
    dialog.show()
    app.processEvents()
    assert dialog.objectName() == "settingsDialog"
    assert len(dialog.findChildren(QWidget, "settingsCard")) == 3
    assert not window.delete_button.isEnabled()
    dialog.backup_button.click()
    dialog.restore_button.click()
    assert called == [("backup", dialog), ("restore", dialog)]
    assert dialog.isVisible()
    dialog.close()
    window.close()
    del app
