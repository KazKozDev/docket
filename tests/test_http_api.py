"""The HTTP API: single-document processing, batch jobs with status and
downloads, upload limits, structured errors, idempotency, restart resume
and the review workflow — against the real pipeline with the LLM stubbed."""
from __future__ import annotations

import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from docket import api, config, extract as extract_module, job_store, review_queue

INVOICE = (
    "INVOICE\nInvoice no: INV-7\nDate: 2026-03-02\nFrom: Acme GmbH\nTo: Beta SA\n"
    "Widget | 2 | 50.00 | 100.00\nSubtotal: 100.00\nVAT: 21.00\nTotal: 121.00\n"
)
PAYLOAD = {
    "invoice_number": "INV-7",
    "issue_date": "2026-03-02",
    "seller": {"name": "Acme GmbH"},
    "buyer": {"name": "Beta SA"},
    "currency": "EUR",
    "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0, "total": 100.0}],
    "subtotal": 100.0,
    "tax_amount": 21.0,
    "total_amount": 121.0,
    "field_locations": {
        "invoice_number": {"page": 1, "quote": "Invoice no: INV-7"},
        "issue_date": {"page": 1, "quote": "Date: 2026-03-02"},
        "seller.name": {"page": 1, "quote": "From: Acme GmbH"},
        "buyer.name": {"page": 1, "quote": "To: Beta SA"},
        "subtotal": {"page": 1, "quote": "Subtotal: 100.00"},
        "tax_amount": {"page": 1, "quote": "VAT: 21.00"},
        "total_amount": {"page": 1, "quote": "Total: 121.00"},
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", None)
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "REVIEW_QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "REVIEW_QUEUE_PATH", tmp_path / "reviews.jsonl")
    monkeypatch.setattr(config, "REVIEW_DOCUMENTS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "OCR_FALLBACKS", [])
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: PAYLOAD)
    with TestClient(api.app) as c:
        yield c


def _wait(client, job_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def _incoming(tmp_path):
    path = tmp_path / "jobs" / "_incoming"
    return list(path.iterdir()) if path.exists() else []


# ---- single document ------------------------------------------------------------


def test_process_runs_the_pipeline(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INCLUDE_LAYOUT", False)
    response = client.post("/process", files={"file": ("inv.txt", INVOICE.encode())},
                           data={"document_type": "invoice"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "inv.txt"
    assert body["status"] == "succeeded" and body["schema_id"] == "invoice"
    assert body["layout"] is None  # include_layout follows DOCKET_INCLUDE_LAYOUT
    assert _incoming(tmp_path) == []  # the upload is gone


def test_process_can_include_layout(client):
    body = client.post("/process", files={"file": ("inv.txt", INVOICE.encode())},
                       data={"include_layout": "true"}).json()
    assert body["layout"]["pages"][0]["text"].startswith("INVOICE")


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"document_type": "spaceship"}, "unknown schema 'spaceship'"),
        ({"ocr_backend": "nope"}, "unknown OCR backend 'nope'"),
        ({"ocr_languages": "klingon"}, "unknown OCR language"),
        ({"schema_version": "9.9", "document_type": "invoice"}, "has no version 9.9"),
    ],
)
def test_invalid_options_are_422_before_anything_is_stored(client, tmp_path, data, fragment):
    response = client.post("/process", files={"file": ("inv.txt", INVOICE.encode())}, data=data)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_options" and fragment in error["message"]
    assert _incoming(tmp_path) == []


def test_arbitrary_code_cannot_be_named(client):
    # There is no `schema` field: only registered schema ids are accepted.
    response = client.post("/process", files={"file": ("inv.txt", INVOICE.encode())},
                           data={"document_type": "os:system"})
    assert response.status_code == 422


def test_unsupported_type_is_415(client, tmp_path):
    response = client.post("/process", files={"file": ("macro.docm", b"PK")})
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"
    assert _incoming(tmp_path) == []


def test_oversized_upload_is_413_and_leaves_nothing_behind(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_BYTES", 10)
    response = client.post("/process", files={"file": ("inv.txt", INVOICE.encode())})
    assert response.status_code == 413 and response.json()["error"]["code"] == "file_too_large"
    assert _incoming(tmp_path) == []


def test_empty_upload_is_400(client):
    response = client.post("/process", files={"file": ("inv.txt", b"")})
    assert response.status_code == 400 and response.json()["error"]["code"] == "empty_file"


def test_api_key_is_enforced_only_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "secret")
    denied = client.get("/schemas")
    assert denied.status_code == 401
    assert denied.json() == {"error": {"code": "unauthorized", "message": "missing or invalid API key"}}
    assert client.get("/schemas", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/schemas", headers={"X-API-Key": "secret"}).status_code == 200
    assert client.get("/health").status_code == 200


# ---- batch jobs -----------------------------------------------------------------------


def _files(*names_and_bodies):
    return [("files", (name, body)) for name, body in names_and_bodies]


def test_batch_job_upload_status_and_downloads(client, tmp_path):
    files = _files(("a.txt", INVOICE.encode()), ("blank.txt", b"   \n"), ("c.txt", INVOICE.encode()))
    created = client.post("/jobs", files=files, data={"document_type": "invoice"})
    assert created.status_code == 202, created.text
    job = _wait(client, created.json()["job_id"])

    assert job["status"] == "completed"
    assert job["counts"] == {"total": 3, "done": 3, "succeeded": 2, "needs_review": 0, "failed": 1}
    assert [d["filename"] for d in job["documents"]] == ["a.txt", "blank.txt", "c.txt"]
    assert not job_store.uploads_dir(job["job_id"]).exists()  # uploads removed when done

    one = client.get(f"/jobs/{job['job_id']}/results/1").json()
    assert one["source"] == "blank.txt" and one["status"] == "failed" and one["error"]["code"] == "no_text"

    jsonl = client.get(f"/jobs/{job['job_id']}/results.jsonl")
    assert jsonl.headers["x-docket-job-status"] == "completed"
    lines = [json.loads(line) for line in jsonl.text.splitlines()]
    assert [r["source"] for r in lines] == ["a.txt", "blank.txt", "c.txt"]

    rows = list(csv.DictReader(io.StringIO(client.get(f"/jobs/{job['job_id']}/results.csv").text)))
    assert [r["status"] for r in rows] == ["succeeded", "failed", "succeeded"]
    assert rows[0]["issuer"] == "Acme GmbH" and rows[0]["total_amount"] == "121.0"
    assert rows[1]["error_code"] == "no_text"

    items = list(csv.DictReader(io.StringIO(client.get(f"/jobs/{job['job_id']}/line-items.csv").text)))
    assert len(items) == 2 and items[0]["document_id"] == rows[0]["document_id"]
    assert items[0]["description"] == "Widget"


def test_batch_limits(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAX_BATCH_FILES", 2)
    response = client.post("/jobs", files=_files(("a.txt", b"x"), ("b.txt", b"x"), ("c.txt", b"x")))
    assert response.status_code == 413 and response.json()["error"]["code"] == "too_many_files"

    monkeypatch.setattr(config, "MAX_BATCH_FILES", 10)
    monkeypatch.setattr(config, "MAX_BATCH_BYTES", len(INVOICE) + 5)
    response = client.post("/jobs", files=_files(("a.txt", INVOICE.encode()), ("b.txt", INVOICE.encode())))
    assert response.status_code == 413 and response.json()["error"]["code"] == "batch_too_large"
    assert _incoming(tmp_path) == []  # the first, accepted file was cleaned up too


def test_idempotency_key_returns_the_same_job(client):
    headers = {"Idempotency-Key": "abc-1"}
    first = client.post("/jobs", files=_files(("a.txt", INVOICE.encode())), headers=headers).json()
    second = client.post("/jobs", files=_files(("a.txt", INVOICE.encode())), headers=headers).json()
    assert first["job_id"] == second["job_id"]
    _wait(client, first["job_id"])


def test_unknown_job_and_document(client):
    assert client.get("/jobs/job_nothere").status_code == 404
    job = client.post("/jobs", files=_files(("a.txt", INVOICE.encode()))).json()
    _wait(client, job["job_id"])
    missing = client.get(f"/jobs/{job['job_id']}/results/5")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "document_not_found"


def test_result_of_an_unfinished_document_is_409(client, tmp_path):
    staged = job_store.incoming_dir() / "x.txt"
    staged.write_text(INVOICE)
    job, _ = job_store.create([("a.txt", staged)], job_store.JobOptions())
    response = client.get(f"/jobs/{job.job_id}/results/0")
    assert response.status_code == 409 and response.json()["error"]["code"] == "not_ready"


def test_restarted_job_resumes_without_redoing_finished_documents(client, tmp_path, monkeypatch):
    import asyncio

    from docket import pipeline

    staged = []
    for n in range(3):
        path = job_store.incoming_dir() / f"s{n}.txt"
        path.write_text(INVOICE.replace("INV-7", f"INV-{n}"))
        staged.append((f"doc{n}.txt", path))
    job, _ = job_store.create(staged, job_store.JobOptions(document_type="invoice"))
    first = job_store.document_paths(job)[0]
    # The server died after finishing the first document.
    done = pipeline.process_document(first, api.resolve(api._process_options(job.options)))
    job_store.results_path(job.job_id).write_text(done.model_dump_json() + "\n")
    job_store.update(job.job_id, status="running")

    processed = []
    real = pipeline.process_document
    monkeypatch.setattr("docket.batch.process_document", lambda p, o: processed.append(p.name) or real(p, o))
    finished = asyncio.run(api._run_job(job.job_id))
    assert finished.status == "completed"
    assert processed == ["00001.txt", "00002.txt"]
    assert [r.source for _, r in job_store.results(finished)] == ["doc0.txt", "doc1.txt", "doc2.txt"]


# ---- catalog & review ---------------------------------------------------------------


def test_listing_endpoints(client):
    assert {s["schema_id"] for s in client.get("/schemas").json()} >= {"invoice", "utility_bill"}
    assert client.get("/schemas/invoice/json-schema").json()["type"] == "object"
    assert {b["name"] for b in client.get("/ocr-backends").json()} >= {"pdf_text", "tesseract", "vlm"}
    templates = {t["template_id"] for t in client.get("/vendor-templates").json()}
    assert "nordlicht-buerobedarf-invoice" in templates
    template = client.get("/vendor-templates/nordlicht-buerobedarf-invoice").json()
    assert template["schema_id"] == "invoice" and template["builtin"] is True
    assert client.get("/vendor-templates/not-here").status_code == 404
    formats = {f["name"]: f for f in client.get("/export-formats").json()}
    assert formats["ubl"]["media_type"] == "application/xml"


def test_review_api_exposes_original_and_accepts_decision(client, tmp_path):
    from tests.factories import make_result

    source = tmp_path / "doc.txt"
    source.write_text("source document")
    document_id = review_queue.enqueue(make_result(source=str(source)), ["manual check"])
    original = client.get(f"/review-queue/{document_id}/original")
    decision = client.patch(f"/review-queue/{document_id}",
                            json={"status": "approved", "actor": "reviewer-1", "corrections": {}})
    bad = client.patch(f"/review-queue/{document_id}", json={"status": "nonsense"})
    assert original.content == b"source document"
    assert decision.status_code == 200 and decision.json()["status"] == "approved"
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_review_update"
    assert client.get("/review-queue/doc_nope").status_code == 404
