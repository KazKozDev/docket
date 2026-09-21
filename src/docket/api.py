"""FastAPI service with bounded concurrency, durable jobs and review workflow.

Run it with `docket-api` (installed by `pip install "docket[api]"`) or
`uvicorn docket.api:app`.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import __version__, config, job_store, review_queue
from . import catalog
from .catalog import SchemaError, SchemaInfo
from .export import list_exporters
from .logging_setup import configure, get_logger
from .pdf import page_count
from .ocr import list_ocr_backends
from .pipeline import process_document
from .result import DocumentResult

configure()
log = get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for job in job_store.unfinished():
        _schedule(job["job_id"])
    yield


app = FastAPI(
    title="docket",
    description="Extract, classify and validate structured data from business documents.",
    version=__version__,
    lifespan=lifespan,
)

_SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".txt", ".md"}
_job_slots = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
_active_tasks: set[asyncio.Task] = set()
_active_by_id: dict[str, asyncio.Task] = {}


class ReviewUpdate(BaseModel):
    status: str
    corrections: dict | None = None
    actor: str = "reviewer"
    note: str | None = None


def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    if config.API_KEY is None:
        return
    bearer = authorization.removeprefix("Bearer ") if authorization else None
    if x_api_key != config.API_KEY and bearer != config.API_KEY:
        raise HTTPException(401, "missing or invalid API key")


async def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise HTTPException(400, f"unsupported file type: {suffix!r}")
    config.JOB_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.JOB_UPLOADS_DIR / f"{uuid4().hex}{suffix}"
    size = 0
    try:
        with path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_FILE_BYTES:
                    raise HTTPException(413, f"file exceeds {config.MAX_FILE_BYTES} byte limit")
                handle.write(chunk)
        if suffix == ".pdf":
            pages = page_count(path)
            if pages > config.MAX_PDF_PAGES:
                raise HTTPException(
                    413, f"PDF has {pages} pages; limit is {config.MAX_PDF_PAGES}"
                )
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def _run_job(job_id: str) -> dict:
    job = job_store.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] == "completed":
        return job
    async with _job_slots:
        job_store.update(job_id, status="running", error=None)
        try:
            result = await run_in_threadpool(process_document, Path(job["path"]))
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline failed", extra={"job_id": job_id})
            return job_store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        return job_store.update(
            job_id, status="completed", result=result.model_dump(mode="json"), error=None
        )


def _schedule(job_id: str) -> asyncio.Task:
    existing = _active_by_id.get(job_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(_run_job(job_id))
    _active_tasks.add(task)
    _active_by_id[job_id] = task

    def _finished(done: asyncio.Task) -> None:
        _active_tasks.discard(done)
        _active_by_id.pop(job_id, None)

    task.add_done_callback(_finished)
    return task


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/schemas", response_model=list[SchemaInfo], dependencies=[Depends(require_api_key)])
def list_schemas(all_versions: bool = False) -> list[SchemaInfo]:
    """Registered schemas (latest version of each unless all_versions)."""
    return [s.info() for s in catalog.list_schemas(all_versions=all_versions)]


def _schema(schema_id: str, version: str | None):
    try:
        return catalog.require_schema(schema_id, version)
    except SchemaError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/schemas/{schema_id}", response_model=SchemaInfo, dependencies=[Depends(require_api_key)])
def get_schema(schema_id: str, version: str | None = None) -> SchemaInfo:
    return _schema(schema_id, version).info()


@app.get("/schemas/{schema_id}/json-schema", dependencies=[Depends(require_api_key)])
def get_json_schema(schema_id: str, version: str | None = None) -> dict:
    """The JSON Schema extraction fills for this schema."""
    return _schema(schema_id, version).json_schema()


@app.get("/export-formats", dependencies=[Depends(require_api_key)])
def export_formats() -> list[dict]:
    return [
        {
            "name": e.name,
            "description": e.description,
            "media_type": e.media_type,
            "accepts": [t.__name__ for t in e.accepts],
            "schemas": [s.schema_id for s in catalog.list_schemas() if issubclass(s.model, e.accepts)],
        }
        for e in list_exporters()
    ]


@app.get("/ocr-backends", dependencies=[Depends(require_api_key)])
def ocr_backends() -> list[dict]:
    """OCR backends this deployment knows, with capabilities and availability."""
    return [info.model_dump(mode="json") for info in list_ocr_backends()]


@app.post("/process", response_model=DocumentResult, dependencies=[Depends(require_api_key)])
async def process_upload(
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DocumentResult:
    path = await _save_upload(file)
    job = job_store.create(path, file.filename or path.name, idempotency_key=idempotency_key)
    if Path(job["path"]) != path:
        path.unlink(missing_ok=True)
    job = await _schedule(job["job_id"])
    if job["status"] != "completed":
        raise HTTPException(500, f"pipeline failed: {job['error']}")
    return DocumentResult.model_validate(job["result"])


@app.post("/jobs", status_code=202, dependencies=[Depends(require_api_key)])
async def create_job(
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    path = await _save_upload(file)
    job = job_store.create(path, file.filename or path.name, idempotency_key=idempotency_key)
    if Path(job["path"]) != path:
        path.unlink(missing_ok=True)
    if job["status"] in {"queued", "running"}:
        _schedule(job["job_id"])
    return job


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> dict:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@app.get("/review-queue", dependencies=[Depends(require_api_key)])
def get_review_queue() -> list[dict]:
    return review_queue.list_pending()


@app.get("/review-queue/{document_id}", dependencies=[Depends(require_api_key)])
def get_review(document_id: str) -> dict:
    record = review_queue.get(document_id)
    if record is None:
        raise HTTPException(404, "review record not found")
    return record


@app.get("/review-queue/{document_id}/original", dependencies=[Depends(require_api_key)])
def get_review_original(document_id: str) -> FileResponse:
    record = review_queue.get(document_id)
    if record is None or not record.get("original_path"):
        raise HTTPException(404, "preserved original not found")
    path = Path(record["original_path"])
    if not path.is_file():
        raise HTTPException(404, "preserved original not found")
    return FileResponse(path, filename=path.name)


@app.patch("/review-queue/{document_id}", dependencies=[Depends(require_api_key)])
def update_review(document_id: str, update: ReviewUpdate) -> dict:
    try:
        return review_queue.update(document_id, **update.model_dump())
    except KeyError as exc:
        raise HTTPException(404, "review record not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def run() -> None:
    """Console entry point: `docket-api [--host H] [--port P]`."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the docket HTTP API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
