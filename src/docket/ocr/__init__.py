"""Pluggable OCR: every backend returns the same PageLayout, and a per-page
fallback chain picks the cheapest reading that can be trusted."""
from .acquire import (
    AUTO_ORDER,
    Acquisition,
    AcquisitionError,
    AcquisitionOptions,
    AcquisitionReport,
    Attempt,
    PageAcquisition,
    acquire,
    resolve_chain,
)
from .base import (
    BackendStatus,
    BackendUnavailable,
    Capabilities,
    OcrBackend,
    OcrError,
    OcrSettings,
)
from .languages import UnknownLanguage, parse_languages
from .registry import (
    ENTRY_POINT_GROUP,
    BackendInfo,
    OcrBackendError,
    get_ocr_backend,
    list_ocr_backends,
    register_ocr_backend,
    unregister_ocr_backend,
)
from .source import SUPPORTED_SUFFIXES, DocumentSource, PageSource, UnsupportedDocument

__all__ = [
    "AUTO_ORDER",
    "Acquisition",
    "AcquisitionError",
    "AcquisitionOptions",
    "AcquisitionReport",
    "Attempt",
    "BackendInfo",
    "BackendStatus",
    "BackendUnavailable",
    "Capabilities",
    "DocumentSource",
    "ENTRY_POINT_GROUP",
    "OcrBackend",
    "OcrBackendError",
    "OcrError",
    "OcrSettings",
    "PageAcquisition",
    "PageSource",
    "SUPPORTED_SUFFIXES",
    "UnknownLanguage",
    "UnsupportedDocument",
    "acquire",
    "get_ocr_backend",
    "list_ocr_backends",
    "parse_languages",
    "register_ocr_backend",
    "resolve_chain",
    "unregister_ocr_backend",
]
