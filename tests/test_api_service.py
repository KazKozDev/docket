import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from docket import api, job_store, review_queue
from docket.schemas import ClassificationResult, DocType, PipelineResult


class _Upload:
    filename = "doc.txt"

    def __init__(self, content: bytes):
        self.content = content
        self.sent = False

    async def read(self, _size: int) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        return self.content


def _result(path: Path) -> PipelineResult:
    return PipelineResult(
        source=str(path),
        classification=ClassificationResult(
            doc_type=DocType.UNKNOWN, confidence=0.0, method="unavailable"
        ),
        extracted=None,
        extract_attempts=0,
        validation_issues=[],
        ocr_method="pdf_text",
        raw_text_chars=1,
    )


def test_api_key_is_enforced_only_when_configured(monkeypatch):
    monkeypatch.setattr(api.config, "API_KEY", "secret")
    with pytest.raises(HTTPException) as exc:
        api.require_api_key(None, None)
    assert exc.value.status_code == 401
    api.require_api_key("Bearer secret", None)


def test_upload_limit_rejects_and_removes_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(api.config, "JOB_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(api.config, "MAX_FILE_BYTES", 3)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api._save_upload(_Upload(b"toolarge")))
    assert exc.value.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_durable_job_runs_pipeline_off_event_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store.config, "JOB_STORE_PATH", tmp_path / "jobs.json")
    source = tmp_path / "doc.txt"
    source.write_text("x")
    job = job_store.create(source, source.name)
    monkeypatch.setattr(api, "process", _result)

    completed = asyncio.run(api._run_job(job["job_id"]))

    assert completed["status"] == "completed"
    assert completed["result"]["source"] == str(source)


def test_review_api_exposes_original_and_accepts_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store.config, "JOB_STORE_PATH", tmp_path / "jobs.json")
    monkeypatch.setattr(
        review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "reviews.jsonl"
    )
    monkeypatch.setattr(
        review_queue.config, "REVIEW_DOCUMENTS_DIR", tmp_path / "originals"
    )
    monkeypatch.setattr(api.config, "API_KEY", None)
    source = tmp_path / "doc.txt"
    source.write_text("source document")
    result = _result(source)
    document_id = review_queue.enqueue(result, ["manual check"])

    with TestClient(api.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        original = client.get(f"/review-queue/{document_id}/original")
        decision = client.patch(
            f"/review-queue/{document_id}",
            json={"status": "approved", "actor": "reviewer-1", "corrections": {}},
        )

    assert original.content == b"source document"
    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"
