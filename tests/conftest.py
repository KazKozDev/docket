"""Shared fixtures."""
from __future__ import annotations

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
