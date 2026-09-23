from __future__ import annotations

import inspect
import json
from pathlib import Path

import docket
import pytest
from docket import (
    DocumentError,
    DocumentResult,
    OcrOptions,
    ProcessingMetrics,
    ProcessOptions,
    ReviewOptions,
)
from docket._deprecation import warn_deprecated


SNAPSHOT = Path(__file__).parent / "snapshots" / "public_api.json"
KEY_FUNCTIONS = (
    "export_document",
    "get_ocr_backend",
    "get_schema",
    "list_exporters",
    "list_ocr_backends",
    "list_schemas",
    "process_batch",
    "process_document",
    "register_exporter",
    "register_ocr_backend",
    "register_schema",
    "validate_einvoice",
)
KEY_MODELS = (
    OcrOptions,
    ReviewOptions,
    ProcessOptions,
    DocumentError,
    ProcessingMetrics,
    DocumentResult,
)


def _annotation_name(annotation: object) -> str:
    text = str(annotation)
    for prefix in ("<class '", "<enum '"):
        if text.startswith(prefix) and text.endswith("'>"):
            return text[len(prefix):-2]
    return text


def _contract() -> dict[str, object]:
    return {
        "names": sorted(docket.__all__),
        "functions": {
            name: str(inspect.signature(getattr(docket, name)))
            for name in KEY_FUNCTIONS
        },
        "models": {
            model.__name__: {
                name: {
                    "annotation": _annotation_name(field.annotation),
                    "required": field.is_required(),
                }
                for name, field in model.model_fields.items()
            }
            for model in KEY_MODELS
        },
    }


def test_public_api_matches_snapshot():
    expected = json.loads(SNAPSHOT.read_text())
    assert _contract() == expected


def test_all_contains_only_bound_names():
    assert not [name for name in docket.__all__ if not hasattr(docket, name)]


def test_deprecation_warning_points_to_caller():
    with pytest.warns(
        DeprecationWarning,
        match=r"old_api is deprecated; use new_api instead; it will be removed in 0\.5",
    ) as caught:
        warn_deprecated("old_api", replacement="new_api", removal="0.5")

    assert caught[0].filename == __file__
