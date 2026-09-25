"""Native, single-user invoice and receipt desk. Run with `docket-desktop`."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from docket import config, pdf
from docket.options import OcrOptions, ProcessOptions
from docket.pipeline import process_document
from docket.result import DocumentResult

from .store import SUPPORTED, DesktopStore, default_data_dir

WINDOW_STYLE = """
QMainWindow, QWidget#workspace { background: #F0F3F3; color: #20313A; }
QWidget#actionBar, QWidget#card {
    background: #FFFFFF; border: 1px solid #DDE5E7; border-radius: 16px;
}
QLabel#brand { color: #17343D; font-size: 25px; font-weight: 750; }
QLabel#toolbarBrand { color: #17343D; font-size: 17px; font-weight: 750; }
QLabel#subtitle, QLabel#muted, QLabel#statusText, QLabel#summary {
    color: #667B83; font-size: 12px;
}
QLabel#sectionTitle { color: #1D343B; font-size: 16px; font-weight: 750; }
QLabel#count, QLabel#stateBadge {
    background: #E6F3EE; color: #0A7666; border-radius: 9px;
    padding: 4px 10px; font-size: 11px; font-weight: 750;
}
QLabel#stateBadge[state="new"] { background: #EEF1F2; color: #6B7B82; }
QLabel#stateBadge[state="failed"] { background: #FCEBE8; color: #AE4A40; }
QLabel#stateBadge[state="approved"] { background: #E7F3E9; color: #27794B; }
QPushButton {
    background: #FFFFFF; color: #24434B; border: 1px solid #D6E0E2;
    border-radius: 8px; padding: 6px 11px; font-size: 12px; font-weight: 650;
}
QPushButton:hover { background: #F1F6F5; border-color: #A5C4BE; }
QPushButton:pressed { background: #E2EFEB; }
QPushButton:focus { border-color: #18816F; }
QPushButton[variant="primary"] { background: #087D6A; color: white; border-color: #087D6A; }
QPushButton[variant="primary"]:hover { background: #086D5D; border-color: #086D5D; }
QPushButton[variant="quiet"] { background: transparent; border-color: transparent; }
QPushButton[variant="quiet"]:hover { background: #EDF3F2; }
QPushButton[variant="small"] { padding: 3px 7px; min-width: 15px; }
QPushButton[variant="dangerQuiet"] { background: transparent; border-color: transparent; padding: 0px; }
QPushButton[variant="dangerQuiet"]:hover { background: #FCEDEA; border-color: transparent; }
QPushButton:disabled { background: #F5F7F7; color: #A5B1B4; border-color: #E7ECEE; }
QPushButton[variant="primary"]:disabled { background: #B8CECA; color: #F7FAF9; border-color: #B8CECA; }
QPushButton[variant="dangerQuiet"]:disabled { background: transparent; border-color: transparent; }
QLineEdit, QComboBox, QPlainTextEdit {
    background: #FFFFFF; color: #20313A; border: 1px solid #D8E3E4;
    border-radius: 9px; padding: 8px 10px; selection-background-color: #B9E2D6;
}
QComboBox { padding-right: 30px; }
QComboBox::drop-down { width: 28px; border: none; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #118472; }
QListWidget, QTreeWidget {
    background: #FFFFFF; color: #20313A; border: none;
    outline: none; selection-background-color: #E1F2EB;
    selection-color: #0A6658;
}
QListWidget::item { border-bottom: 1px solid #EEF1F1; padding: 8px 11px; }
QListWidget::item:hover { background: #F4F8F7; }
QListWidget::item:selected { background: #E0F2EC; color: #0A6658; border-radius: 9px; }
QTreeWidget::item { padding: 5px 6px; min-height: 25px; border-bottom: 1px solid #EEF2F2; }
QTreeWidget::item:hover { background: #F4F8F7; }
QTreeWidget::item:selected { background: #E1F2EB; color: #0A6658; }
QTreeWidget QLineEdit { padding: 3px 6px; border-radius: 5px; }
QHeaderView::section {
    background: #F5F8F7; color: #627980; border: none;
    border-bottom: 1px solid #E4EBEA; padding: 9px; font-weight: 700;
}
QScrollArea#documentPreview { background: #E7ECEC; border: 1px solid #DFE7E8; border-radius: 11px; }
QLabel#previewImage { background: #E7ECEC; color: #667B83; font-size: 13px; }
QSplitter::handle { background: transparent; width: 8px; }
QStatusBar { background: #F0F3F3; color: #687D83; border-top: 1px solid #E1E8E8; }
QMenu { background: white; color: #20313A; border: 1px solid #D8E3E4; padding: 6px; }
QMenu::item { padding: 8px 22px; border-radius: 6px; }
QMenu::item:selected { background: #E1F2EB; color: #0A6658; }
QDialog#settingsDialog { background: #F0F3F3; color: #20313A; }
QDialog#settingsDialog QLabel { color: #20313A; }
QDialog#settingsDialog QLabel#subtitle, QDialog#settingsDialog QLabel#muted { color: #667B83; }
QWidget#settingsCard { background: white; border: 1px solid #DDE5E7; border-radius: 14px; }
"""


def _trash_icon() -> QIcon:
    pixmap = QPixmap(40, 40)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#A64539"))
    pen.setWidthF(1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    path = QPainterPath()
    path.moveTo(3.5, 5.5)
    path.lineTo(16.5, 5.5)
    path.moveTo(7, 5.5)
    path.lineTo(7.5, 3.5)
    path.lineTo(12.5, 3.5)
    path.lineTo(13, 5.5)
    path.moveTo(5, 7.5)
    path.lineTo(5.8, 16)
    path.lineTo(14.2, 16)
    path.lineTo(15, 7.5)
    path.moveTo(8, 8.5)
    path.lineTo(8.2, 13.5)
    path.moveTo(12, 8.5)
    path.lineTo(11.8, 13.5)
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


def _set_path(root: dict, path: tuple[str | int, ...], value: object) -> None:
    cursor: object = root
    for part in path[:-1]:
        cursor = cursor[part]  # type: ignore[index]
    cursor[path[-1]] = value  # type: ignore[index]


def _parse_value(text: str, original: object) -> object:
    if not text.strip():
        return None if original is None else ""
    if isinstance(original, bool):
        return text.strip().lower() in {"true", "yes", "1"}
    if isinstance(original, int) and not isinstance(original, bool):
        return int(text)
    if isinstance(original, float):
        return float(text.replace(",", "."))
    return text


def _suggested_save_path(filename: str) -> str:
    documents = Path.home() / "Documents"
    folder = documents if documents.is_dir() else Path.home()
    return str(folder / filename)


class ProcessWorker(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, document_id: str, path: str, ocr_options: OcrOptions):
        super().__init__()
        self.document_id = document_id
        self.path = path
        self.ocr_options = ocr_options

    @Slot()
    def run(self) -> None:
        try:
            result = process_document(
                self.path,
                ProcessOptions(
                    include_layout=True,
                    ocr=self.ocr_options,
                ),
            )
            self.finished.emit(self.document_id, result)
        except Exception as exc:  # noqa: BLE001 - worker boundary must return control to the UI
            self.failed.emit(self.document_id, f"{type(exc).__name__}: {exc}")


class SettingsComboBox(QComboBox):
    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#24434B" if self.isEnabled() else "#A5B1B4"))
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        center_x = self.width() - 17
        center_y = self.height() / 2
        arrow = QPainterPath()
        arrow.moveTo(center_x - 5, center_y - 3)
        arrow.lineTo(center_x, center_y + 3)
        arrow.lineTo(center_x + 5, center_y - 3)
        painter.drawPath(arrow)


class SettingsDialog(QDialog):
    def __init__(self, parent: DesktopWindow, settings: QSettings):
        super().__init__(parent)
        self.setWindowTitle("Docket settings")
        self.setObjectName("settingsDialog")
        self.setStyleSheet(WINDOW_STYLE)
        self.resize(650, 730)
        self.settings = settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(16)
        title = QLabel("Settings")
        title.setObjectName("brand")
        outer.addWidget(title)
        caption = QLabel("Models, OCR and workspace backups.")
        caption.setObjectName("subtitle")
        outer.addWidget(caption)

        model_card = QWidget()
        model_card.setObjectName("settingsCard")
        model_box = QVBoxLayout(model_card)
        model_box.setContentsMargins(18, 18, 18, 18)
        model_box.setSpacing(12)
        model_heading = QLabel("Language model")
        model_heading.setObjectName("sectionTitle")
        model_box.addWidget(model_heading)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(11)
        self.provider = SettingsComboBox()
        self.provider.addItems(["ollama", "openai"])
        self.provider.setMinimumWidth(180)
        self.provider.setCurrentText(str(settings.value("provider", config.LLM_PROVIDER)))
        self.text_model = QLineEdit(str(settings.value("text_model", config.TEXT_MODEL)))
        self.vision_model = QLineEdit(str(settings.value("vision_model", config.VISION_MODEL)))
        self.base_url = QLineEdit(str(settings.value("base_url", config.LLM_BASE_URL)))
        self.ollama_host = QLineEdit(str(settings.value("ollama_host", config.OLLAMA_HOST)))
        self.backend = SettingsComboBox()
        self.backend.addItems(["auto", "tesseract", "paddle", "docling"])
        self.backend.setMinimumWidth(180)
        self.backend.setCurrentText(str(settings.value("backend", "auto")))
        self.languages = QLineEdit(str(settings.value("languages", ",".join(config.OCR_LANGUAGES))))
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Leave blank to keep the saved key")
        form.addRow("Provider", self.provider)
        form.addRow("Text model", self.text_model)
        form.addRow("Vision model", self.vision_model)
        form.addRow("API URL", self.base_url)
        form.addRow("Ollama host", self.ollama_host)
        form.addRow("API key", self.api_key)
        model_box.addLayout(form)
        self.provider_hint = QLabel()
        self.provider_hint.setObjectName("muted")
        self.provider_hint.setWordWrap(True)
        model_box.addWidget(self.provider_hint)
        outer.addWidget(model_card)
        self.provider.currentTextChanged.connect(self._update_provider_fields)
        self._provider_form = form
        self._update_provider_fields()

        ocr_card = QWidget()
        ocr_card.setObjectName("settingsCard")
        ocr_box = QVBoxLayout(ocr_card)
        ocr_box.setContentsMargins(18, 18, 18, 18)
        ocr_box.setSpacing(12)
        ocr_heading = QLabel("OCR")
        ocr_heading.setObjectName("sectionTitle")
        ocr_box.addWidget(ocr_heading)
        ocr_form = QFormLayout()
        ocr_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        ocr_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        ocr_form.setHorizontalSpacing(20)
        ocr_form.setVerticalSpacing(11)
        ocr_form.addRow("Engine", self.backend)
        ocr_form.addRow("Languages", self.languages)
        ocr_box.addLayout(ocr_form)
        language_hint = QLabel("Use two-letter codes separated by commas, for example en, es, de.")
        language_hint.setObjectName("muted")
        ocr_box.addWidget(language_hint)
        outer.addWidget(ocr_card)

        backup_card = QWidget()
        backup_card.setObjectName("settingsCard")
        backup_layout = QVBoxLayout(backup_card)
        backup_layout.setContentsMargins(18, 18, 18, 18)
        backup_layout.setSpacing(10)
        backup_heading = QLabel("Backups")
        backup_heading.setObjectName("sectionTitle")
        backup_layout.addWidget(backup_heading)
        backup_hint = QLabel("Save your documents and review history, or restore a previous backup.")
        backup_hint.setObjectName("muted")
        backup_hint.setWordWrap(True)
        backup_layout.addWidget(backup_hint)
        backup_actions = QHBoxLayout()
        backup_actions.addStretch()
        self.backup_button = QPushButton("Create backup")
        self.backup_button.clicked.connect(lambda: parent.backup(self))
        backup_actions.addWidget(self.backup_button)
        self.restore_button = QPushButton("Restore backup")
        self.restore_button.clicked.connect(lambda: parent.restore(self))
        backup_actions.addWidget(self.restore_button)
        backup_layout.addLayout(backup_actions)
        outer.addWidget(backup_card)
        outer.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        save_button.setText("Save settings")
        save_button.setProperty("variant", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _update_provider_fields(self) -> None:
        is_openai = self.provider.currentText() == "openai"
        self.provider_hint.setText(
            "Documents are sent to the selected API endpoint. The key is stored in the system keychain."
            if is_openai else
            "Ollama :cloud models send documents to a remote service. Local models stay on this computer."
        )
        for widget, visible in ((self.base_url, is_openai), (self.api_key, is_openai),
                                (self.ollama_host, not is_openai)):
            widget.setVisible(visible)
            label = self._provider_form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)

    def accept(self) -> None:
        if not self.text_model.text().strip() or not self.vision_model.text().strip():
            QMessageBox.warning(self, "Missing model", "Enter both model names.")
            return
        for key, value in {
            "provider": self.provider.currentText(), "text_model": self.text_model.text().strip(),
            "vision_model": self.vision_model.text().strip(), "base_url": self.base_url.text().strip(),
            "ollama_host": self.ollama_host.text().strip(), "backend": self.backend.currentText(),
            "languages": self.languages.text().strip(),
        }.items():
            self.settings.setValue(key, value)
        if self.api_key.text():
            try:
                import keyring

                keyring.set_password("docket-desktop", "llm-api-key", self.api_key.text())
            except Exception as exc:  # noqa: BLE001 - keyring backends have platform-specific errors
                QMessageBox.warning(self, "Keychain", f"Could not save API key: {exc}")
                return
        self.settings.sync()
        super().accept()


class JsonDialog(QDialog):
    def __init__(self, parent: QWidget, data: dict):
        super().__init__(parent)
        self.setWindowTitle("Edit all fields")
        self.resize(680, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Edit fields and line items. The schema is checked before approval."))
        self.editor = QPlainTextEdit(json.dumps(data, indent=2, ensure_ascii=False))
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_json)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.data: dict | None = None

    def _accept_json(self) -> None:
        try:
            parsed = json.loads(self.editor.toPlainText())
            if not isinstance(parsed, dict):
                raise TypeError("Expected a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Invalid JSON", str(exc))
            return
        self.data = parsed
        self.accept()


class DesktopWindow(QMainWindow):
    def __init__(self, store: DesktopStore):
        super().__init__()
        self.store = store
        self.settings = QSettings("Docket", "Desktop")
        self.current_id: str | None = None
        self.result: DocumentResult | None = None
        self.working: dict | None = None
        self.page = 1
        self.preview_zoom = 1.0
        self.selected_field: str | None = None
        self.pending: list[str] = []
        self._ocr_override: OcrOptions | None = None
        self._thread: QThread | None = None
        self._worker: ProcessWorker | None = None
        self.setWindowTitle("Docket — Invoices & Receipts")
        self.resize(1500, 900)
        self.setAcceptDrops(True)
        self.setStyleSheet(WINDOW_STYLE)
        self._build()
        self.refresh()

    def _button(self, label: str, callback, variant: str = "") -> QPushButton:
        button = QPushButton(label)
        if variant:
            button.setProperty("variant", variant)
        button.clicked.connect(callback)
        return button

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._render_preview)

    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("workspace")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(22, 18, 22, 12)
        outer.setSpacing(12)

        action_bar = QWidget()
        action_bar.setObjectName("actionBar")
        actions = QHBoxLayout(action_bar)
        actions.setContentsMargins(14, 8, 14, 8)
        actions.setSpacing(8)
        toolbar_brand = QLabel("Docket")
        toolbar_brand.setObjectName("toolbarBrand")
        actions.addWidget(toolbar_brand)
        actions.addSpacing(18)
        self.add_button = QPushButton("Add")
        self.add_button.setProperty("variant", "primary")
        add_menu = QMenu(self.add_button)
        add_menu.addAction("Files…", self.add_files)
        add_menu.addAction("Folder…", self.add_folder)
        self.add_button.setMenu(add_menu)
        actions.addWidget(self.add_button)
        actions.addWidget(self._button("Process", self.process_selected))
        self.vlm_button = self._button("VLM", self.process_selected_vlm)
        self.vlm_button.setToolTip("Read the selected document with the vision model from Settings")
        actions.addWidget(self.vlm_button)
        actions.addStretch()
        actions.addWidget(self._button("Settings", self.open_settings))
        outer.addWidget(action_bar)

        split = QSplitter()
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        outer.addWidget(split, 1)
        sidebar = QWidget()
        sidebar.setObjectName("card")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 18, 18, 18)
        sidebar_layout.setSpacing(12)
        sidebar_heading = QHBoxLayout()
        sidebar_heading.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search documents…")
        self.search.textChanged.connect(self.refresh)
        sidebar_heading.addWidget(self.search, 1)
        self.delete_button = self._button("", self.delete_selected, "dangerQuiet")
        self.delete_button.setIcon(_trash_icon())
        self.delete_button.setIconSize(QSize(20, 20))
        self.delete_button.setFixedSize(28, 28)
        self.delete_button.setAccessibleName("Delete selected document")
        self.delete_button.setToolTip("Delete the selected document")
        self.delete_button.setEnabled(False)
        sidebar_heading.addWidget(self.delete_button)
        self.document_count = QLabel("0")
        self.document_count.setObjectName("count")
        sidebar_heading.addWidget(self.document_count)
        sidebar_layout.addLayout(sidebar_heading)
        self.documents = QListWidget()
        self.documents.setSpacing(2)
        self.documents.currentItemChanged.connect(self.load_selected)
        sidebar_layout.addWidget(self.documents)
        split.addWidget(sidebar)

        preview_panel = QWidget()
        preview_panel.setObjectName("card")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(18, 18, 18, 18)
        preview_layout.setSpacing(12)
        pages = QHBoxLayout()
        pages.addStretch()
        pages.addWidget(self._button("◀", self.previous_page, "small"))
        self.page_label = QLabel("Page 0 / 0")
        self.page_label.setObjectName("muted")
        pages.addWidget(self.page_label)
        pages.addWidget(self._button("▶", self.next_page, "small"))
        pages.addSpacing(8)
        pages.addWidget(self._button("−", self.zoom_out, "small"))
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("muted")
        pages.addWidget(self.zoom_label)
        pages.addWidget(self._button("+", self.zoom_in, "small"))
        preview_layout.addLayout(pages)
        self.preview = QLabel("Add a PDF or image to get started")
        self.preview.setObjectName("previewImage")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_scroll = QScrollArea()
        self._preview_scroll.setObjectName("documentPreview")
        self._preview_scroll.setViewportMargins(6, 6, 6, 6)
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setWidget(self.preview)
        preview_layout.addWidget(self._preview_scroll)
        split.addWidget(preview_panel)

        detail_panel = QWidget()
        detail_panel.setObjectName("card")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(18, 18, 18, 18)
        detail_layout.setSpacing(10)
        detail_heading = QHBoxLayout()
        detail_title = QLabel("Extracted data")
        detail_title.setObjectName("sectionTitle")
        detail_heading.addWidget(detail_title)
        detail_heading.addStretch()
        self.state_badge = QLabel("Waiting")
        self.state_badge.setObjectName("stateBadge")
        detail_heading.addWidget(self.state_badge)
        detail_layout.addLayout(detail_heading)
        self.status = QLabel("Select a document")
        self.status.setObjectName("statusText")
        self.status.setWordWrap(True)
        detail_layout.addWidget(self.status)
        self.summary = QLabel("")
        self.summary.setObjectName("summary")
        self.summary.setWordWrap(True)
        detail_layout.addWidget(self.summary)
        self.fields = QTreeWidget()
        self.fields.setHeaderLabels(["Field", "Value"])
        self.fields.setColumnWidth(0, 180)
        self.fields.setAlternatingRowColors(False)
        self.fields.itemChanged.connect(self.field_changed)
        self.fields.currentItemChanged.connect(self.field_selected)
        detail_layout.addWidget(self.fields, 1)
        self.edit_button = self._button("Edit line items / full data…", self.edit_json)
        detail_layout.addWidget(self.edit_button)
        self.quote = QLabel("")
        self.quote.setObjectName("muted")
        self.quote.setWordWrap(True)
        detail_layout.addWidget(self.quote)
        self.history_label = QLabel("")
        self.history_label.setObjectName("muted")
        self.history_label.setWordWrap(True)
        detail_layout.addWidget(self.history_label)
        detail_layout.addWidget(self._button("View history…", self.show_history, "quiet"))
        review_actions = QHBoxLayout()
        review_actions.setSpacing(8)
        self.save_button = self._button("Save edits", self.save_edits)
        review_actions.addWidget(self.save_button)
        self.approve_button = self._button("Approve", self.approve, "primary")
        review_actions.addWidget(self.approve_button)
        self.export_button = QPushButton("Export ▾")
        export_menu = QMenu(self.export_button)
        for label, handler in (("Excel (.xlsx)", self.export_xlsx),
                               ("CSV (.csv)", self.export_csv),
                               ("JSON (.json)", self.export_json)):
            export_menu.addAction(label).triggered.connect(handler)
        self.export_button.setMenu(export_menu)
        review_actions.addWidget(self.export_button)
        detail_layout.addLayout(review_actions)
        split.addWidget(detail_panel)
        split.setSizes([270, 580, 650])
        split.splitterMoved.connect(lambda _position, _index: self._render_preview())
        self.statusBar().showMessage("All documents require human approval before export")

    def refresh(self) -> None:
        selected = self.current_id
        self.documents.blockSignals(True)
        self.documents.clear()
        rows = self.store.list_documents(self.search.text())
        self.document_count.setText(str(len(rows)))
        self.export_button.setEnabled(any(row["state"] == "approved" for row in self.store.list_documents()))
        for row in rows:
            item = QListWidgetItem(f"{row['filename']}\n{row['kind'] or 'unread'} · {row['state']}")
            item.setSizeHint(QSize(0, 52))
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.documents.addItem(item)
            if row["id"] == selected:
                self.documents.setCurrentItem(item)
        self.documents.blockSignals(False)
        if self.documents.currentItem() is None and self.documents.count():
            self.documents.setCurrentRow(0)
        elif self.documents.currentItem() is None:
            self.delete_button.setEnabled(False)
            self.edit_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.approve_button.setEnabled(False)

    def load_selected(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            self.delete_button.setEnabled(False)
            return
        self.current_id = current.data(Qt.ItemDataRole.UserRole)
        self.delete_button.setEnabled(True)
        row = self.store.get(self.current_id)
        if row is None:
            return
        self.result = DocumentResult.model_validate_json(row["result_json"]) if row["result_json"] else None
        self.working = json.loads(row["edited_json"]) if row["edited_json"] else None
        editable = self.working is not None and row["state"] != "approved"
        self.edit_button.setEnabled(editable)
        self.save_button.setEnabled(editable)
        self.approve_button.setEnabled(editable)
        self.page = 1
        self.selected_field = None
        self.state_badge.setText(row["state"].replace("_", " ").title())
        self.state_badge.setProperty("state", row["state"])
        self.state_badge.style().unpolish(self.state_badge)
        self.state_badge.style().polish(self.state_badge)
        self.status.setText(row["filename"] +
                            (f" · Docket: {self.result.status.value}" if self.result else ""))
        if self.result and self.result.review_reasons:
            self.status.setText(self.status.text() + "\n" + "; ".join(self.result.review_reasons))
        self._update_summary()
        self._populate_fields()
        self._render_preview()
        self._history()

    def _update_summary(self) -> None:
        data = self.working or {}
        seller = data.get("seller")
        merchant = (seller.get("name") if isinstance(seller, dict) else None) or data.get("merchant_name")
        number = data.get("invoice_number")
        total = data.get("total_amount")
        currency = data.get("currency") or ""
        parts = []
        if merchant:
            parts.append(str(merchant))
        if number:
            parts.append(f"#{number}")
        if total is not None:
            parts.append(f"{total} {currency}".strip())
        self.summary.setText("  ·  ".join(parts))

    def _history(self) -> None:
        if self.current_id:
            events = self.store.history(self.current_id)
            self.history_label.setText("History: " + " · ".join(f"{r['action']} {r['at']}" for r in events[-4:]))

    def show_history(self) -> None:
        if not self.current_id:
            return
        events = self.store.history(self.current_id)
        lines = []
        for event in events:
            detail = json.loads(event["detail_json"] or "{}")
            lines.append(f"{event['at']} · {event['action']}" +
                         (f" · {detail['actor']}" if detail.get("actor") else ""))
            for change in detail.get("changes", []):
                lines.append(f"  {change['field']}: {change['before']!r} → {change['after']!r}")
        dialog = QDialog(self)
        dialog.setWindowTitle("Document history")
        dialog.resize(700, 500)
        layout = QVBoxLayout(dialog)
        viewer = QPlainTextEdit("\n".join(lines))
        viewer.setReadOnly(True)
        layout.addWidget(viewer)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _populate_fields(self) -> None:
        self.fields.blockSignals(True)
        self.fields.clear()
        def add(parent: QTreeWidgetItem | None, key: str, value: object,
                path: tuple[str | int, ...]) -> None:
            item = QTreeWidgetItem([key, "" if isinstance(value, (dict, list)) else str(value if value is not None else "")])
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            if parent is None:
                self.fields.addTopLevelItem(item)
            else:
                parent.addChild(item)
            if isinstance(value, dict):
                for name, child in value.items():
                    add(item, name, child, path + (name,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    add(item, str(index + 1), child, path + (index,))
            else:
                item.setData(1, Qt.ItemDataRole.UserRole, value)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        for key, value in (self.working or {}).items():
            add(None, key, value, (key,))
        self.fields.expandToDepth(0)
        self.fields.blockSignals(False)

    def field_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 1 or self.working is None:
            return
        path = tuple(item.data(0, Qt.ItemDataRole.UserRole) or ())
        try:
            value = _parse_value(item.text(1), item.data(1, Qt.ItemDataRole.UserRole))
            _set_path(self.working, path, value)
            self._update_summary()
            self.statusBar().showMessage("Unsaved changes — click Save edits", 5000)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid value", str(exc))

    def field_selected(self, item: QTreeWidgetItem | None, _previous=None) -> None:
        if item is None:
            return
        path = item.data(0, Qt.ItemDataRole.UserRole) or ()
        name = "".join(f"[{p}]" if isinstance(p, int) else ("." if i else "") + p
                       for i, p in enumerate(path))
        self.selected_field = name
        source = self.result.field_sources.get(name) if self.result else None
        self.quote.setText(f"Source: {source.quote}" if source else "No source citation for this field")
        if source and source.page != self.page:
            self.page = source.page
        self._render_preview()

    def _render_preview(self) -> None:
        row = self.store.get(self.current_id) if self.current_id else None
        if row is None:
            return
        path = Path(row["stored_path"])
        try:
            if path.suffix.lower() == ".pdf":
                total = pdf.page_count(path)
                self.page = max(1, min(self.page, total))
                image = pdf.render_page(path, self.page - 1,
                                        min(300, max(120, round(120 * self.preview_zoom))))
            else:
                with Image.open(path) as opened:
                    total = getattr(opened, "n_frames", 1)
                    self.page = max(1, min(self.page, total))
                    opened.seek(self.page - 1)
                    image = opened.convert("RGB")
            image = image.copy()
            source = self.result.field_sources.get(self.selected_field) if self.result and self.selected_field else None
            if source:
                draw = ImageDraw.Draw(image)
                for region in source.regions:
                    if region.page == self.page:
                        box = region.bbox
                        draw.rectangle((box.x0 * image.width, box.y0 * image.height,
                                        box.x1 * image.width, box.y1 * image.height),
                                       outline="#ed6148", width=max(3, image.width // 300))
            target_width = min(2400, max(220, int((self._preview_scroll.viewport().width() - 24)
                                                   * self.preview_zoom)))
            target_height = max(1, round(image.height * target_width / image.width))
            image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
            qimage = QImage(image.tobytes(), image.width, image.height,
                            image.width * 3, QImage.Format.Format_RGB888).copy()
            self.preview.setPixmap(QPixmap.fromImage(qimage))
            self.preview.setMinimumSize(target_width, target_height)
            self.page_label.setText(f"Page {self.page} / {total}")
        except Exception as exc:  # noqa: BLE001 - preview failure must not close the document
            self.preview.setText(f"Preview unavailable: {exc}")

    def previous_page(self) -> None:
        self.page = max(1, self.page - 1)
        self._render_preview()

    def next_page(self) -> None:
        self.page += 1
        self._render_preview()

    def zoom_in(self) -> None:
        self.preview_zoom = min(3.0, round(self.preview_zoom + 0.25, 2))
        self.zoom_label.setText(f"{self.preview_zoom:.0%}")
        self._render_preview()

    def zoom_out(self) -> None:
        self.preview_zoom = max(0.5, round(self.preview_zoom - 0.25, 2))
        self.zoom_label.setText(f"{self.preview_zoom:.0%}")
        self._render_preview()

    def _import(self, paths: list[Path]) -> None:
        added: list[str] = []
        for path in paths:
            try:
                document_id, created = self.store.add_file(path)
                if created:
                    added.append(document_id)
            except ValueError:
                continue
        self.refresh()
        if added:
            self.statusBar().showMessage(f"Imported {len(added)} documents; choose Process to read them")
        else:
            self.statusBar().showMessage("No new PDF or image files found")

    def add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add invoices or receipts", "",
                                                "Documents (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)")
        self._import([Path(file) for file in files])

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add a folder")
        if folder:
            self._import([p for p in Path(folder).rglob("*") if p.suffix.lower() in SUPPORTED])

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        expanded = [file for path in paths for file in (path.rglob("*") if path.is_dir() else [path])
                    if file.suffix.lower() in SUPPORTED]
        self._import(expanded)

    def process_selected(self) -> None:
        self._start_selected_process()

    def process_selected_vlm(self) -> None:
        languages = str(self.settings.value("languages", ",".join(config.OCR_LANGUAGES)))
        self._start_selected_process(
            OcrOptions(backend="vlm", fallbacks=[], use_pdf_text=False, languages=languages.split(","))
        )

    def _start_selected_process(self, ocr_override: OcrOptions | None = None) -> None:
        if self._thread is not None:
            return
        selected = self.documents.currentItem()
        if selected is None:
            return
        ids = [selected.data(Qt.ItemDataRole.UserRole)]
        self.pending = [document_id for document_id in ids
                        if (row := self.store.get(document_id)) and row["state"] != "approved"]
        self._ocr_override = ocr_override
        self._process_next()

    def _process_next(self) -> None:
        if not self.pending:
            self._ocr_override = None
            self.statusBar().showMessage("Processing finished")
            self.refresh()
            return
        document_id = self.pending.pop(0)
        row = self.store.get(document_id)
        if row is None:
            self._process_next()
            return
        mode = " with VLM" if self._ocr_override is not None else ""
        self.statusBar().showMessage(f"Processing {row['filename']}{mode}…")
        self._thread = QThread()
        languages = str(self.settings.value("languages", ",".join(config.OCR_LANGUAGES)))
        ocr_options = self._ocr_override or OcrOptions(
            backend=str(self.settings.value("backend", "auto")), languages=languages.split(",")
        )
        worker = ProcessWorker(document_id, row["stored_path"], ocr_options)
        self._worker = worker
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.finished.connect(self._finished)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._thread.quit)
        worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(worker.deleteLater)
        self._thread.finished.connect(self._thread_done)
        self._thread.start()

    @Slot()
    def _thread_done(self) -> None:
        assert self._thread is not None
        self._thread.deleteLater()
        self._thread = None
        self._worker = None
        self._process_next()

    @Slot(str, object)
    def _finished(self, document_id: str, result: DocumentResult) -> None:
        self.store.save_result(document_id, result)
        self.refresh()
        if self.current_id == document_id:
            self.load_selected(self.documents.currentItem())

    @Slot(str, str)
    def _failed(self, document_id: str, message: str) -> None:
        self.statusBar().showMessage(f"Processing failed: {message}")
        QMessageBox.warning(self, "Processing failed", f"{document_id[:12]}: {message}")

    def save_edits(self) -> bool:
        if not self.current_id or self.working is None:
            return False
        try:
            self.store.save_edits(self.current_id, self.working)
            self.statusBar().showMessage("Edits saved")
            self._history()
            return True
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return False

    def edit_json(self) -> None:
        if self.working is None:
            return
        dialog = JsonDialog(self, self.working)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.data is not None:
            self.working = dialog.data
            self._populate_fields()
            self._update_summary()
            self.statusBar().showMessage("Unsaved changes — click Save edits")

    def approve(self) -> None:
        if not self.current_id or self.working is None:
            return
        if not self.save_edits():
            return
        try:
            errors = self.store.approve(self.current_id)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot approve", str(exc))
            return
        if errors:
            QMessageBox.warning(self, "Check these fields", "\n".join(errors[:12]))
            return
        self.refresh()
        self.load_selected(self.documents.currentItem())
        self.statusBar().showMessage("Document approved; it can now be exported")

    def export_csv(self) -> None:
        self._export("csv")

    def export_xlsx(self) -> None:
        self._export("xlsx")

    def export_json(self) -> None:
        self._export("json")

    def _export(self, format_name: str) -> None:
        filters = {"csv": "CSV (*.csv)", "xlsx": "Excel workbook (*.xlsx)", "json": "JSON (*.json)"}
        path, _ = QFileDialog.getSaveFileName(
            self, "Export approved documents",
            _suggested_save_path(f"approved-documents.{format_name}"), filters[format_name],
        )
        if not path:
            return
        try:
            if format_name == "csv":
                count, items = self.store.export_approved(Path(path))
                detail = (f"\nAdditional CSV files beside it: {items.name}, "
                          f"{Path(path).stem}-fields.csv, {Path(path).stem}-processing.csv, "
                          f"{Path(path).stem}-history.csv")
            elif format_name == "xlsx":
                count = self.store.export_approved_xlsx(Path(path))
                detail = ""
            else:
                count = self.store.export_approved_json(Path(path))
                detail = ""
            QMessageBox.information(self, "Exported", f"{count} approved documents exported to {path}.{detail}")
        except (OSError, ImportError) as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def backup(self, dialog_parent: QWidget | None = None) -> None:
        owner = dialog_parent or self
        path, _ = QFileDialog.getSaveFileName(owner, "Save backup",
                                              _suggested_save_path("docket-backup.zip"), "ZIP (*.zip)")
        if path:
            try:
                self.store.backup(Path(path))
                QMessageBox.information(owner, "Backup", f"Backup saved to {path}")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(owner, "Backup", str(exc))

    def restore(self, dialog_parent: QWidget | None = None) -> None:
        owner = dialog_parent or self
        if self._thread is not None:
            QMessageBox.information(owner, "Processing", "Wait for the current document to finish.")
            return
        path, _ = QFileDialog.getOpenFileName(owner, "Restore backup", "", "ZIP (*.zip)")
        if not path:
            return
        if QMessageBox.question(owner, "Restore backup", "Replace current documents with this backup? A copy of the current data will be kept beside the app data folder.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.restore_backup(Path(path))
            self.current_id = None
            self.result = None
            self.working = None
            self.refresh()
            self.statusBar().showMessage("Backup restored")
        except (OSError, ValueError, zipfile.BadZipFile, sqlite3.DatabaseError) as exc:
            QMessageBox.warning(owner, "Restore failed", str(exc))

    def delete_selected(self) -> None:
        if not self.current_id:
            return
        if self._thread is not None:
            QMessageBox.information(self, "Processing", "Wait for the current document to finish.")
            return
        if QMessageBox.question(self, "Delete document", "Delete this document, its result and history permanently?") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete(self.current_id)
        self.current_id = None
        self.result = None
        self.working = None
        self.fields.clear()
        self.preview.setPixmap(QPixmap())
        self.preview.setText("Add a PDF or image to get started")
        self.refresh()

    def open_settings(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "Processing", "Wait for the current document to finish.")
            return
        if SettingsDialog(self, self.settings).exec() == QDialog.DialogCode.Accepted:
            _apply_settings(self.settings)

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "Processing", "Wait for the current document to finish.")
            event.ignore()
            return
        self.store.close()
        super().closeEvent(event)


def _apply_settings(settings: QSettings) -> None:
    for key, attribute in (
        ("provider", "LLM_PROVIDER"), ("text_model", "TEXT_MODEL"),
        ("vision_model", "VISION_MODEL"), ("base_url", "LLM_BASE_URL"),
        ("ollama_host", "OLLAMA_HOST"),
    ):
        value = settings.value(key)
        if value:
            setattr(config, attribute, str(value))
    try:
        import keyring

        secret = keyring.get_password("docket-desktop", "llm-api-key")
        if secret:
            config.LLM_API_KEY = secret
    except Exception as exc:  # noqa: BLE001 - keyring backend can be unavailable
        import logging

        logging.getLogger("docket").warning("Desktop keychain unavailable: %s", type(exc).__name__)


def run() -> None:
    if getattr(sys, "frozen", False) and sys.platform == "darwin" and not shutil.which("tesseract"):
        for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
            if Path(candidate).is_file():
                import pytesseract

                pytesseract.pytesseract.tesseract_cmd = candidate
                break
    app = QApplication(sys.argv)
    app.setApplicationName("Docket Desktop")
    icon_path = Path(__file__).parent / "assets" / "icon.png"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    config.configure_app()
    _apply_settings(QSettings("Docket", "Desktop"))
    store = DesktopStore(default_data_dir())
    window = DesktopWindow(store)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
