"""The surface third-party applications build on: export registry, CLI,
pluggable LLM backend, PDF rendering and the opt-out review queue."""
import json

import pypdfium2 as pdfium
import pytest

from docket import config, llm_client, pdf
from docket import cli
from docket.export import (
    ExportError,
    export_document,
    get_exporter,
    list_exporters,
    register_exporter,
)
from docket.schemas import Invoice

from tests.test_export import sample_bank_statement, sample_invoice


def test_eu_formats_are_registered():
    names = {e.name for e in list_exporters()}
    assert {"ubl", "zugferd", "xrechnung", "facturae"} <= names


def test_export_by_name_matches_direct_call():
    from docket.export import export_to_ubl_xml

    invoice = sample_invoice()
    exported = export_document(invoice, "ubl")
    assert exported.content == export_to_ubl_xml(invoice)
    assert exported.media_type == "application/xml"


def test_json_exporters_return_text():
    parsed = json.loads(export_document(sample_invoice(), "xero-json").content)
    assert isinstance(parsed, dict)


def test_wrong_document_type_is_rejected():
    with pytest.raises(ExportError, match="requires Invoice"):
        export_document(sample_bank_statement(), "xrechnung")


def test_unknown_format_is_rejected():
    with pytest.raises(ExportError, match="unknown export format"):
        get_exporter("no-such-format")


def test_custom_exporter_can_be_registered(monkeypatch):
    import docket.export as export_module

    monkeypatch.setattr(export_module, "_REGISTRY", dict(export_module._REGISTRY))
    register_exporter("my-erp", lambda inv: f"#{inv.invoice_number}", accepts=(Invoice,))
    assert export_document(sample_invoice(), "my-erp").content == "#INV-2026-001"
    with pytest.raises(ExportError, match="already registered"):
        register_exporter("my-erp", str, accepts=(Invoice,))


def test_cli_lists_formats(capsys):
    cli.main(["--list-formats"])
    out = capsys.readouterr().out
    assert "xrechnung" in out and "ubl" in out


def test_cli_requires_document():
    with pytest.raises(SystemExit):
        cli.main([])


class _Response:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


def test_openai_compatible_backend(monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ANN001
        captured.update(url=url, payload=json, headers=headers)
        return _Response({"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://api.mistral.ai/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    monkeypatch.setattr(llm_client.httpx, "post", fake_post)

    assert llm_client.chat_json("hi", model="mistral-small-latest") == {"ok": True}
    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer k"}
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["model"] == "mistral-small-latest"


def test_openai_vision_sends_data_url(monkeypatch, tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"img")
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ANN001
        captured.update(json)
        return _Response({"choices": [{"message": {"content": " text "}}]})

    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm_client.httpx, "post", fake_post)

    assert llm_client.vision_transcribe(image.read_bytes(), model="pixtral-12b") == "text"
    parts = captured["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_pdf_render_and_count(tmp_path):
    path = tmp_path / "blank.pdf"
    document = pdfium.PdfDocument.new()
    document.new_page(595, 842)
    document.new_page(595, 842)
    document.save(str(path))
    document.close()

    assert pdf.page_count(path) == 2
    image = pdf.render_page(path, 1, dpi=72)
    assert image.mode == "RGB" and image.size == (595, 842)
    assert len(pdf.render_pages(path, dpi=36)) == 2


def test_pipeline_result_exposes_typed_document():
    from tests.factories import make_result

    invoice = sample_invoice()
    result = make_result(source="x.pdf", extracted=invoice.model_dump(mode="json"))
    assert isinstance(result.document, Invoice)
    assert result.document.invoice_number == invoice.invoice_number
    assert result.model_copy(update={"extracted": None}).document is None


def test_cli_exports_extracted_document(monkeypatch, capsys):
    from tests.factories import make_result

    result = make_result(source="x.pdf", extracted=sample_invoice().model_dump(mode="json"))
    monkeypatch.setattr(cli, "process_document", lambda _path, _options: result)
    cli.main(["x.pdf", "--export", "ubl"])
    assert "INV-2026-001" in capsys.readouterr().out


def test_review_queue_can_be_disabled_per_call(monkeypatch, tmp_path):
    from docket import pipeline, review_queue
    from docket.schemas import ClassificationResult, DocType
    from tests.factories import make_result, text_acquisition

    source = tmp_path / "x.txt"
    source.write_text("x")
    flagged = make_result(
        source=str(source),
        classification=ClassificationResult(doc_type=DocType.INVOICE, confidence=0.1, method="rules"),
        extracted=None,
    )
    monkeypatch.setattr(pipeline, "_acquire", lambda *a, **k: text_acquisition("x"))
    monkeypatch.setattr(pipeline, "_read", lambda *a, **k: flagged)
    enqueued: list = []
    monkeypatch.setattr(review_queue, "enqueue", lambda *a, **k: enqueued.append(a) or "id")

    from docket.options import OcrOptions, ProcessOptions, ReviewOptions

    def run(enqueue):
        return pipeline.process_document(
            source, ProcessOptions(ocr=OcrOptions(fallbacks=[]), review=ReviewOptions(enqueue=enqueue))
        )

    result = run(False)
    assert result.needs_review and enqueued == []
    run(True)
    assert len(enqueued) == 1
