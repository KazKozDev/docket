from datetime import date

import pymupdf as fitz

from docket import extract as extract_module
from docket import ocr
from docket.schemas import Invoice


def _invoice_payload() -> dict:
    return {
        "invoice_number": "INV-1",
        "issue_date": date(2026, 1, 1).isoformat(),
        "vendor_name": "Acme",
        "customer_name": "Bob",
        "subtotal": 10.0,
        "total_amount": 10.0,
    }


def test_long_document_processes_tail_and_merges_page_candidates(monkeypatch):
    prompts: list[str] = []

    def fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        if "Merge the page-level" in prompt:
            return _invoice_payload()
        return {"invoice_number": "INV-1"}

    monkeypatch.setattr(extract_module, "chat_json", fake_chat)
    monkeypatch.setattr(extract_module.config, "EXTRACT_CHUNK_CHARS", 1000)
    pages = ["start " + "a" * 1100, "b" * 1100 + " UNIQUE_END"]

    result, attempts = extract_module.extract_pages(pages, Invoice, max_retries=0)

    assert result is not None
    assert attempts > 2
    assert any("UNIQUE_END" in prompt for prompt in prompts)
    assert all("[PAGE " in prompt for prompt in prompts if "Document chunk" in prompt)
    merge_prompts = [p for p in prompts if "Merge the page-level" in p]
    assert merge_prompts and "UNIQUE_END" in merge_prompts[0]


def test_partial_calls_request_schema_constrained_json(monkeypatch):
    seen: dict = {}

    def fake_chat(prompt, **kwargs):
        seen.update(kwargs)
        if "Merge the page-level" in prompt:
            return _invoice_payload()
        return {"invoice_number": "INV-1"}

    monkeypatch.setattr(extract_module, "chat_json", fake_chat)
    monkeypatch.setattr(extract_module.config, "EXTRACT_CHUNK_CHARS", 1000)
    extract_module.extract_pages(["start " + "a" * 1100, "b" * 1100], Invoice, max_retries=0)
    assert isinstance(seen.get("schema"), dict)


def test_retry_prompt_keeps_source_document(monkeypatch):
    prompts: list[str] = []

    def fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return {} if len(prompts) == 1 else _invoice_payload()

    monkeypatch.setattr(extract_module, "chat_json", fake_chat)
    result, attempts = extract_module.extract("SOURCE_SENTINEL", Invoice, max_retries=1)
    assert result is not None
    assert attempts == 2
    assert "SOURCE_SENTINEL" in prompts[1]


def test_mixed_pdf_uses_text_layer_and_ocr_per_page(tmp_path, monkeypatch):
    path = tmp_path / "mixed.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Digital page with enough searchable text")
        document.new_page()
        document.save(path)

    monkeypatch.setattr(ocr, "_render_pdf_page", lambda *_args: object())
    monkeypatch.setattr(ocr, "_ocr_image", lambda _image: ("Scanned second page", 99.0, []))
    result = ocr.extract_text(path)

    assert result.method == "mixed"
    assert result.page_methods == ["pdf_text", "ocr"]
    assert "Digital page" in result.pages[0]
    assert result.pages[1] == "Scanned second page"
