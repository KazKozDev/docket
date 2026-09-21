"""Durable HTTP jobs: one directory per job.

    <DOCKET_JOBS_DIR>/<job_id>/
        job.json         metadata, options, per-document index, counts
        results.jsonl    finished DocumentResults (the batch checkpoint)
        uploads/         the uploaded files, deleted when the job finishes

A job is a batch of one or more uploaded documents. Results are appended as
each document finishes, so a restarted server resumes a job where it
stopped (the batch engine skips documents already in results.jsonl) and
nothing but job.json is ever rewritten.
"""
from __future__ import annotations

import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from . import config
from .result import DocumentResult

_LOCK = threading.RLock()
JobStatus = Literal["queued", "running", "completed", "failed"]


class JobOptions(BaseModel):
    """What an HTTP caller may choose. Only registered schemas can be named —
    the API never imports or runs caller-supplied code."""

    document_type: str | None = None
    schema_version: str | None = None
    ocr_backend: str | None = None
    ocr_fallbacks: list[str] | None = None
    ocr_languages: str | None = None
    include_layout: bool = False


class JobDocument(BaseModel):
    index: int
    filename: str
    stored_as: str
    size: int


class JobCounts(BaseModel):
    total: int = 0
    done: int = 0
    succeeded: int = 0
    needs_review: int = 0
    failed: int = 0


class Job(BaseModel):
    job_id: str
    status: JobStatus = "queued"
    created_at: str
    updated_at: str
    idempotency_key: str | None = None
    options: JobOptions = Field(default_factory=JobOptions)
    documents: list[JobDocument] = Field(default_factory=list)
    counts: JobCounts = Field(default_factory=JobCounts)
    error: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def root() -> Path:
    return Path(config.JOBS_DIR)


def job_dir(job_id: str) -> Path:
    if not job_id.startswith("job_") or not job_id[4:].isalnum():
        raise KeyError(job_id)  # never let an id become a path outside the store
    return root() / job_id


def uploads_dir(job_id: str) -> Path:
    return job_dir(job_id) / "uploads"


def results_path(job_id: str) -> Path:
    return job_dir(job_id) / "results.jsonl"


def incoming_dir() -> Path:
    """Where uploads stream to before their job exists."""
    path = root() / "_incoming"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(job: Job) -> Job:
    path = job_dir(job.job_id) / "job.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return job


def find_by_key(idempotency_key: str) -> Job | None:
    for job in all_jobs():
        if job.idempotency_key == idempotency_key:
            return job
    return None


def create(
    files: list[tuple[str, Path]], options: JobOptions, *, idempotency_key: str | None = None
) -> tuple[Job, bool]:
    """Move staged uploads into a new job. Returns (job, created); with a
    known idempotency key the existing job comes back and the staged files
    are discarded."""
    with _LOCK:
        if idempotency_key:
            existing = find_by_key(idempotency_key)
            if existing is not None:
                for _, staged in files:
                    staged.unlink(missing_ok=True)
                return existing, False
        job_id = f"job_{uuid4().hex}"
        target = uploads_dir(job_id)
        target.mkdir(parents=True)
        documents = []
        for index, (filename, staged) in enumerate(files):
            stored = target / f"{index:05d}{staged.suffix}"
            size = staged.stat().st_size
            shutil.move(str(staged), stored)
            documents.append(JobDocument(index=index, filename=filename, stored_as=stored.name, size=size))
        job = Job(
            job_id=job_id,
            created_at=_now(),
            updated_at=_now(),
            idempotency_key=idempotency_key,
            options=options,
            documents=documents,
            counts=JobCounts(total=len(documents)),
        )
        return _write(job), True


def get(job_id: str) -> Job | None:
    try:
        path = job_dir(job_id) / "job.json"
    except KeyError:
        return None
    if not path.exists():
        return None
    return Job.model_validate_json(path.read_text(encoding="utf-8"))


def update(job_id: str, **changes) -> Job:
    with _LOCK:
        job = get(job_id)
        if job is None:
            raise KeyError(job_id)
        job = job.model_copy(update={**changes, "updated_at": _now()})
        return _write(job)


def all_jobs() -> list[Job]:
    if not root().exists():
        return []
    jobs = []
    for path in sorted(root().glob("job_*/job.json")):
        try:
            jobs.append(Job.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return jobs


def unfinished() -> list[Job]:
    return [job for job in all_jobs() if job.status in ("queued", "running")]


def document_paths(job: Job) -> list[Path]:
    return [uploads_dir(job.job_id) / d.stored_as for d in job.documents]


def results(job: Job) -> Iterator[tuple[JobDocument, DocumentResult]]:
    """Finished results in document order, each with its upload record.
    The result's `source` is the caller's filename, not the storage path."""
    path = results_path(job.job_id)
    if not path.exists():
        return
    by_stored: dict[str, DocumentResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            result = DocumentResult.model_validate_json(line)
        except ValueError:
            continue
        by_stored[Path(result.source).name] = result
    for document in job.documents:
        result = by_stored.get(document.stored_as)
        if result is not None:
            yield document, result.model_copy(update={"source": document.filename})


def remove_uploads(job_id: str) -> None:
    shutil.rmtree(uploads_dir(job_id), ignore_errors=True)


__all__ = [
    "Job",
    "JobCounts",
    "JobDocument",
    "JobOptions",
    "all_jobs",
    "create",
    "document_paths",
    "get",
    "incoming_dir",
    "remove_uploads",
    "results",
    "results_path",
    "unfinished",
    "update",
]
