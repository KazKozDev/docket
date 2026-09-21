"""Small durable job registry used by the API's restart-safe queue."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, dict]:
    if not config.JOB_STORE_PATH.exists():
        return {}
    return json.loads(config.JOB_STORE_PATH.read_text(encoding="utf-8"))


def _write(jobs: dict[str, dict]) -> None:
    config.JOB_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.JOB_STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.JOB_STORE_PATH)


def create(path: Path, filename: str, *, idempotency_key: str | None = None) -> dict:
    with _LOCK:
        jobs = _read()
        if idempotency_key:
            existing = next(
                (
                    job
                    for job in jobs.values()
                    if job.get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if existing:
                return existing
        job_id = f"job_{uuid4().hex}"
        job = {
            "job_id": job_id,
            "status": "queued",
            "filename": filename,
            "path": str(path),
            "idempotency_key": idempotency_key,
            "created_at": _now(),
            "updated_at": _now(),
            "result": None,
            "error": None,
        }
        jobs[job_id] = job
        _write(jobs)
        return job


def get(job_id: str) -> dict | None:
    with _LOCK:
        return _read().get(job_id)


def update(job_id: str, **changes) -> dict:
    with _LOCK:
        jobs = _read()
        if job_id not in jobs:
            raise KeyError(job_id)
        jobs[job_id].update(changes, updated_at=_now())
        _write(jobs)
        return jobs[job_id]


def unfinished() -> list[dict]:
    with _LOCK:
        return [
            job for job in _read().values() if job["status"] in {"queued", "running"}
        ]
