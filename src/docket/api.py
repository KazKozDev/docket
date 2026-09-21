"""HTTP API: the same contract as the Python API and the CLI.

    POST /process                     one document, synchronous → DocumentResult
    POST /jobs                        one or more documents, asynchronous → 202 Job
    POST /validate/einvoice           official EN 16931 / Peppol / XRechnung / Factur-X validation
    GET  /jobs/{id}                   status, counts, per-document progress
    GET  /jobs/{id}/results/{index}   one DocumentResult
    GET  /jobs/{id}/results.jsonl     every finished result, JSON Lines
    GET  /jobs/{id}/results.csv       summary CSV (see docket.export.tabular)
    GET  /jobs/{id}/line-items.csv    line-item CSV, linked by document_id
    GET  /schemas, /schemas/{id}, /schemas/{id}/json-schema
    GET  /ocr-backends, /export-formats
    GET  /review-queue ...            human review workflow

Uploads stream to disk under random names (only the suffix of the original
name is kept), are checked against the size, file-count and page limits,
and are deleted when their job finishes. Callers can only name registered
schemas — the API never imports caller-supplied code. Every error is
`{"error": {"code": ..., "message": ...}}` with a matching status code.

Run with `docket-api` (`pip install "docket-idp[api]"`) or
`uvicorn docket.api:app`.
"""
from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, catalog, config, job_store, review_queue
from .batch import BatchOptions, process_batch
from .catalog import SchemaError, SchemaInfo
from .einvoice import EInvoiceUnavailable, EInvoiceValidationOptions, EInvoiceValidationResult, Profile
from .einvoice import validate_einvoice as run_einvoice_validation
from .errors import ConfigurationError
from .export import list_exporters
from .export import tabular
from .job_store import Job, JobOptions
from .logging_setup import configure, get_logger
from .ocr import SUPPORTED_SUFFIXES, list_ocr_backends
from .options import OcrOptions, ProcessOptions, resolve
from .pdf import page_count
from .pipeline import process_document
from .result import DocumentResult, DocumentStatus

configure()
log = get_logger()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


_ERRORS = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Not ready"},
    413: {"model": ErrorResponse, "description": "Too large"},
    415: {"model": ErrorResponse, "description": "Unsupported file type"},
    422: {"model": ErrorResponse, "description": "Invalid options"},
    503: {"model": ErrorResponse, "description": "Feature not installed on this server"},
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _job_slots
    # Invalid settings stop the server before it accepts a request.
    config.check()
    _job_slots = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
    for job in job_store.unfinished():
        _schedule(job.job_id)
    yield


app = FastAPI(
    title="docket",
    description="Extract, classify, validate and export structured data from business documents.",
    version=__version__,
    lifespan=lifespan,
)

_job_slots = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
_active: dict[str, asyncio.Task] = {}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(EInvoiceUnavailable)
async def _einvoice_unavailable(_request: Request, exc: EInvoiceUnavailable) -> JSONResponse:
    return _error(503, "einvoice_unavailable", str(exc))


@app.exception_handler(ConfigurationError)
async def _configuration_error(_request: Request, exc: ConfigurationError) -> JSONResponse:
    return _error(422, "invalid_options", str(exc))


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return _error(422, "invalid_request", details)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error(exc.status_code, code, str(exc.detail))


def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    if config.API_KEY is None:
        return
    bearer = authorization.removeprefix("Bearer ") if authorization else None
    if x_api_key != config.API_KEY and bearer != config.API_KEY:
        raise ApiError(401, "unauthorized", "missing or invalid API key")


# ---- uploads ------------------------------------------------------------------


async def _stage(file: UploadFile, budget: list[int]) -> Path:
    """Stream one upload to a random name, enforcing the per-file and the
    remaining per-request byte limits. Deleted on any failure."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ApiError(415, "unsupported_file_type", f"{file.filename!r}: unsupported file type {suffix or '(none)'}")
    path = job_store.incoming_dir() / f"{uuid4().hex}{suffix}"
    size = 0
    try:
        with path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_FILE_BYTES:
                    raise ApiError(413, "file_too_large", f"{file.filename!r} exceeds {config.MAX_FILE_BYTES} bytes")
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise ApiError(413, "batch_too_large", f"uploads exceed {config.MAX_BATCH_BYTES} bytes in total")
                handle.write(chunk)
        if size == 0:
            raise ApiError(400, "empty_file", f"{file.filename!r} is empty")
        if suffix == ".pdf":
            try:
                pages = page_count(path)
            except Exception as exc:  # noqa: BLE001 — pdfium raises its own error type
                raise ApiError(400, "unreadable_pdf", f"{file.filename!r} is not a readable PDF") from exc
            if pages > config.MAX_PDF_PAGES:
                raise ApiError(413, "too_many_pages", f"{file.filename!r} has {pages} pages; limit is {config.MAX_PDF_PAGES}")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _stage_all(files: list[UploadFile]) -> list[tuple[str, Path]]:
    if not files:
        raise ApiError(400, "no_files", "upload at least one file")
    if len(files) > config.MAX_BATCH_FILES:
        raise ApiError(413, "too_many_files", f"{len(files)} files; limit is {config.MAX_BATCH_FILES}")
    budget = [config.MAX_BATCH_BYTES]
    staged: list[tuple[str, Path]] = []
    try:
        for file in files:
            staged.append((Path(file.filename or "upload").name, await _stage(file, budget)))
        return staged
    except BaseException:
        for _, path in staged:
            path.unlink(missing_ok=True)
        raise


def _job_options(
    document_type: str | None,
    schema_version: str | None,
    ocr_backend: str | None,
    ocr_fallbacks: str | None,
    ocr_languages: str | None,
    include_layout: bool | None,
) -> JobOptions:
    fallbacks = None if ocr_fallbacks is None else [f.strip() for f in ocr_fallbacks.split(",") if f.strip()]
    return JobOptions(
        document_type=document_type or None,
        schema_version=schema_version or None,
        ocr_backend=ocr_backend or None,
        ocr_fallbacks=fallbacks,
        ocr_languages=ocr_languages or None,
        include_layout=config.INCLUDE_LAYOUT if include_layout is None else include_layout,
    )


def _process_options(options: JobOptions) -> ProcessOptions:
    return ProcessOptions(
        ocr=OcrOptions(backend=options.ocr_backend, fallbacks=options.ocr_fallbacks, languages=options.ocr_languages),
        document_type=options.document_type,
        schema_version=options.schema_version,
        include_layout=options.include_layout,
    )


# Form fields shared by /process and /jobs.
DocumentTypeForm = Form(default=None, description="Registered schema id; skips classification.")
SchemaVersionForm = Form(default=None, description="Registered version of document_type.")
OcrBackendForm = Form(default=None, description="Primary OCR backend name.")
OcrFallbacksForm = Form(default=None, description="Comma-separated fallback backends; empty string for none.")
OcrLanguagesForm = Form(default=None, description="ISO 639-1 codes, comma-separated.")
IncludeLayoutForm = Form(default=None, description="Keep page layouts in results; default DOCKET_INCLUDE_LAYOUT.")


# ---- jobs ------------------------------------------------------------------------


def _run_batch(job: Job) -> None:
    resolved = resolve(_process_options(job.options))
    counts = job.counts.model_copy(update={"done": 0, "succeeded": 0, "needs_review": 0, "failed": 0})

    def progress(_index: int, result: DocumentResult) -> None:
        counts.done += 1
        if result.status == DocumentStatus.FAILED:
            counts.failed += 1
        elif result.status == DocumentStatus.NEEDS_REVIEW:
            counts.needs_review += 1
        else:
            counts.succeeded += 1
        job_store.update(job.job_id, counts=counts)

    process_batch(
        job_store.document_paths(job),
        resolved,
        BatchOptions(
            workers=config.BATCH_WORKERS,
            checkpoint=job_store.results_path(job.job_id),
            keep_results=False,
        ),
        on_result=progress,
    )


async def _run_job(job_id: str) -> Job:
    job = job_store.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.status in ("completed", "failed"):
        return job
    async with _job_slots:
        job = job_store.update(job_id, status="running", error=None)
        try:
            await run_in_threadpool(_run_batch, job)
        except Exception as exc:  # noqa: BLE001 — a job must end in a state, never hang
            log.exception("job failed", extra={"job_id": job_id})
            job = job_store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        else:
            job = job_store.update(job_id, status="completed")
        finally:
            job_store.remove_uploads(job_id)
        return job


def _schedule(job_id: str) -> asyncio.Task:
    existing = _active.get(job_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(_run_job(job_id))
    _active[job_id] = task
    task.add_done_callback(lambda _t: _active.pop(job_id, None))
    return task


def _job(job_id: str) -> Job:
    job = job_store.get(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", f"no job {job_id!r}")
    return job


# ---- endpoints ---------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.post("/process", response_model=DocumentResult, responses=_ERRORS, dependencies=[Depends(require_api_key)])
async def process_upload(
    file: UploadFile = File(...),
    document_type: str | None = DocumentTypeForm,
    schema_version: str | None = SchemaVersionForm,
    ocr_backend: str | None = OcrBackendForm,
    ocr_fallbacks: str | None = OcrFallbacksForm,
    ocr_languages: str | None = OcrLanguagesForm,
    include_layout: bool | None = IncludeLayoutForm,
) -> DocumentResult:
    """Process one document and wait for the result."""
    options = _job_options(document_type, schema_version, ocr_backend, ocr_fallbacks, ocr_languages, include_layout)
    resolved = resolve(_process_options(options))  # 422 before the upload is stored
    [(filename, path)] = await _stage_all([file])
    try:
        async with _job_slots:
            result = await run_in_threadpool(process_document, path, resolved)
    finally:
        path.unlink(missing_ok=True)
    return result.model_copy(update={"source": filename})


@app.post("/jobs", status_code=202, response_model=Job, responses=_ERRORS, dependencies=[Depends(require_api_key)])
async def create_job(
    files: list[UploadFile] = File(..., description="One or more documents."),
    document_type: str | None = DocumentTypeForm,
    schema_version: str | None = SchemaVersionForm,
    ocr_backend: str | None = OcrBackendForm,
    ocr_fallbacks: str | None = OcrFallbacksForm,
    ocr_languages: str | None = OcrLanguagesForm,
    include_layout: bool | None = IncludeLayoutForm,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Job:
    """Queue documents for processing; poll `GET /jobs/{job_id}`. The same
    Idempotency-Key returns the existing job instead of a new one."""
    options = _job_options(document_type, schema_version, ocr_backend, ocr_fallbacks, ocr_languages, include_layout)
    resolve(_process_options(options))
    staged = await _stage_all(files)
    job, created = job_store.create(staged, options, idempotency_key=idempotency_key)
    if created or job.status in ("queued", "running"):
        _schedule(job.job_id)
    return job


@app.get("/jobs/{job_id}", response_model=Job, responses=_ERRORS, dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> Job:
    return _job(job_id)


@app.get(
    "/jobs/{job_id}/results/{index}",
    response_model=DocumentResult,
    responses=_ERRORS,
    dependencies=[Depends(require_api_key)],
)
def get_job_result(job_id: str, index: int) -> DocumentResult:
    job = _job(job_id)
    if not 0 <= index < len(job.documents):
        raise ApiError(404, "document_not_found", f"job has documents 0..{len(job.documents) - 1}")
    for document, result in job_store.results(job):
        if document.index == index:
            return result
    raise ApiError(409, "not_ready", f"document {index} is not processed yet (job {job.status})")


def _download(job: Job, lines: Iterator[str], media_type: str, filename: str) -> StreamingResponse:
    return StreamingResponse(
        lines,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Docket-Job-Status": job.status,
            "X-Docket-Documents-Done": str(job.counts.done),
        },
    )


@app.get("/jobs/{job_id}/results.jsonl", responses=_ERRORS, dependencies=[Depends(require_api_key)])
def download_jsonl(job_id: str) -> StreamingResponse:
    """Finished results so far, in document order. Check X-Docket-Job-Status
    for whether the job is complete."""
    job = _job(job_id)
    layout = job.options.include_layout
    lines = (tabular.jsonl_line(result, include_layout=layout) for _, result in job_store.results(job))
    return _download(job, lines, "application/x-ndjson", f"{job_id}.jsonl")


def _csv_lines(rows: Iterator[dict], columns: tuple[str, ...]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = tabular.CsvWriter(buffer, columns)
    for row in rows:
        writer.write(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
    yield buffer.getvalue()  # the header, when there were no rows


@app.get("/jobs/{job_id}/results.csv", responses=_ERRORS, dependencies=[Depends(require_api_key)])
def download_csv(job_id: str) -> StreamingResponse:
    job = _job(job_id)
    rows = (tabular.result_row(result) for _, result in job_store.results(job))
    return _download(job, _csv_lines(rows, tabular.RESULT_COLUMNS), "text/csv", f"{job_id}.csv")


@app.get("/jobs/{job_id}/line-items.csv", responses=_ERRORS, dependencies=[Depends(require_api_key)])
def download_line_items(job_id: str) -> StreamingResponse:
    job = _job(job_id)
    rows = (row for _, result in job_store.results(job) for row in tabular.line_item_rows(result))
    return _download(job, _csv_lines(rows, tabular.ITEM_COLUMNS), "text/csv", f"{job_id}.line_items.csv")


@app.post(
    "/validate/einvoice",
    response_model=EInvoiceValidationResult,
    responses=_ERRORS,
    dependencies=[Depends(require_api_key)],
)
async def validate_einvoice_upload(
    file: UploadFile = File(..., description="UBL or CII XML, or a Factur-X / ZUGFeRD PDF."),
    profile: Profile | None = Form(default=None, description="Validate as this profile; default: the declared one."),
) -> EInvoiceValidationResult:
    """Validate an e-invoice with the official XSD and Schematron rules."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xml", ".pdf"}:
        raise ApiError(415, "unsupported_file_type", f"{file.filename!r}: send .xml or .pdf")
    data = await file.read(config.MAX_FILE_BYTES + 1)
    if len(data) > config.MAX_FILE_BYTES:
        raise ApiError(413, "file_too_large", f"{file.filename!r} exceeds {config.MAX_FILE_BYTES} bytes")
    if not data:
        raise ApiError(400, "empty_file", f"{file.filename!r} is empty")
    return await run_in_threadpool(run_einvoice_validation, data, EInvoiceValidationOptions(profile=profile))


@app.get("/schemas", response_model=list[SchemaInfo], dependencies=[Depends(require_api_key)])
def list_schemas(all_versions: bool = False) -> list[SchemaInfo]:
    """Registered schemas (latest version of each unless all_versions)."""
    return [s.info() for s in catalog.list_schemas(all_versions=all_versions)]


def _schema(schema_id: str, version: str | None):
    try:
        return catalog.require_schema(schema_id, version)
    except SchemaError as exc:
        raise ApiError(404, "schema_not_found", str(exc)) from exc


@app.get("/schemas/{schema_id}", response_model=SchemaInfo, responses=_ERRORS, dependencies=[Depends(require_api_key)])
def get_schema(schema_id: str, version: str | None = None) -> SchemaInfo:
    return _schema(schema_id, version).info()


@app.get("/schemas/{schema_id}/json-schema", responses=_ERRORS, dependencies=[Depends(require_api_key)])
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


# ---- review queue ----------------------------------------------------------------


class ReviewUpdate(BaseModel):
    status: str
    corrections: dict | None = None
    actor: str = "reviewer"
    note: str | None = None


@app.get("/review-queue", dependencies=[Depends(require_api_key)])
def get_review_queue() -> list[dict]:
    return review_queue.list_pending()


@app.get("/review-queue/{document_id}", responses=_ERRORS, dependencies=[Depends(require_api_key)])
def get_review(document_id: str) -> dict:
    record = review_queue.get(document_id)
    if record is None:
        raise ApiError(404, "review_not_found", "review record not found")
    return record


@app.get("/review-queue/{document_id}/original", responses=_ERRORS, dependencies=[Depends(require_api_key)])
def get_review_original(document_id: str) -> FileResponse:
    record = review_queue.get(document_id)
    if record is None or not record.get("original_path"):
        raise ApiError(404, "original_not_found", "preserved original not found")
    path = Path(record["original_path"])
    if not path.is_file():
        raise ApiError(404, "original_not_found", "preserved original not found")
    return FileResponse(path, filename=path.name)


@app.patch("/review-queue/{document_id}", responses=_ERRORS, dependencies=[Depends(require_api_key)])
def update_review(document_id: str, update: ReviewUpdate) -> dict:
    try:
        return review_queue.update(document_id, **update.model_dump())
    except KeyError as exc:
        raise ApiError(404, "review_not_found", "review record not found") from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_review_update", str(exc)) from exc


def run() -> None:
    """Console entry point: `docket-api [--host H] [--port P] [--config PATH]`."""
    import argparse
    import sys

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the docket HTTP API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", metavar="PATH",
                        help="TOML settings file (default: DOCKET_CONFIG, else ./docket.toml if present)")
    args = parser.parse_args()
    if args.config:
        config.configure(args.config)
    try:
        config.check()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
    uvicorn.run(app, host=args.host, port=args.port)
