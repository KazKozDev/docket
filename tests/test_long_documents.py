from datetime import date

import pymupdf as fitz

from docket import extract as extract_module
from docket.catalog import Invoice


def _invoice_payload() -> dict:
    return {
        "invoice_number": "INV-1",
        "issue_date": date(2026, 1, 1).isoformat(),
        "seller": {"name": "Acme"},
        "buyer": {"name": "Bob"},
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
    extract_module.extract_pages(
        ["start " + "a" * 1100, "b" * 1100], Invoice, max_retries=0
    )
    assert isinstance(seen.get("schema"), dict)


def test_retry_prompt_keeps_source_document(monkeypatch):
    prompts: list[str] = []

    def fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return {} if len(prompts) == 1 else _invoice_payload()

    monkeypatch.setattr(extract_module, "chat_json", fake_chat)
    result, attempts = extract_module.extract_pages(["SOURCE_SENTINEL"], Invoice, max_retries=1)
    assert result is not None
    assert attempts == 2
    assert "SOURCE_SENTINEL" in prompts[1]


def test_date_convention_instruction_in_extraction_prompt(monkeypatch):
    prompts: list[str] = []

    def fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return _invoice_payload()

    monkeypatch.setattr(extract_module, "chat_json", fake_chat)
    text = "[PAGE 1]\nInvoice date 11/02/2019\nDue 26/02/2019"
    extract_module.extract_pages([text], Invoice, max_retries=0)
    assert any("uses DMY" in p for p in prompts)


def test_decimal_comma_document_gets_the_day_first_instruction(monkeypatch):
    prompts: list[str] = []

    def fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return _invoice_payload()

    monkeypatch.setattr(extract_module, "chat_json", fake_chat)
    extract_module.extract_pages(["[PAGE 1]\nDate : 03/09/2026\nTotal TTC : 484,80 €"], Invoice, max_retries=0)
    assert any("uses DMY" in p for p in prompts)
