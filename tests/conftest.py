"""Shared fixtures."""
from __future__ import annotations

import os

# Hermetic LLM settings, set before docket reads the environment: tests stub
# the model calls, and a model a developer happens to run locally must never
# answer the ones they don't stub (tests would pass there and fail in CI).
os.environ.update({
    "DOCKET_LLM_PROVIDER": "ollama",
    "OLLAMA_HOST": "http://127.0.0.1:9",
    "DOCKET_TEXT_MODEL": "test-text-model",
    "DOCKET_VISION_MODEL": "test-vision-model",
    "DOCKET_LLM_RETRIES": "0",
})

import pytest

from docket import export as export_module
from docket.catalog import Invoice, Receipt


@pytest.fixture
def json_format(monkeypatch) -> str:
    """A plain JSON export format, for tests of export_document itself rather
    than of any one format. Registered for this test only."""
    monkeypatch.setattr(export_module, "_REGISTRY", dict(export_module._REGISTRY))
    export_module.register_exporter(
        "plain-json", lambda doc: doc.model_dump(mode="json"), accepts=(Invoice, Receipt),
        media_type="application/json",
    )
    return "plain-json"
