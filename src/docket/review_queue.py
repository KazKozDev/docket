"""Transactional human-review workflow backed by SQLite or PostgreSQL."""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
)
from sqlalchemy import update as sql_update
from sqlalchemy.engine import Engine

from . import config
from .result import DocumentResult

_LOCK = threading.RLock()
_STATUSES = {"pending", "in_review", "corrected", "approved", "rejected"}
_ENGINES: dict[str, Engine] = {}
_METADATA = MetaData()
_TASKS = Table(
    "review_tasks", _METADATA,
    Column("document_id", String(80), primary_key=True), Column("status", String(24), nullable=False, index=True),
    Column("queued_at", String(40), nullable=False), Column("updated_at", String(40), nullable=False),
    Column("source", Text, nullable=False), Column("original_path", Text), Column("doc_type", String(100)),
    Column("reasons", JSON, nullable=False), Column("result", JSON, nullable=False), Column("corrections", JSON),
    Column("validation_issues", JSON, nullable=False, default=list), Column("lock_owner", String(200)),
    Column("lock_token", String(80)), Column("lock_expires_at", Float),
    Column("version", Integer, nullable=False, default=1),
)
_REVISIONS = Table(
    "review_revisions", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", String(80), nullable=False, index=True), Column("at", String(40), nullable=False),
    Column("action", String(40), nullable=False), Column("actor", String(200)), Column("note", Text),
    Column("corrections", JSON), Column("validation_issues", JSON), Column("version", Integer, nullable=False),
)


class ReviewConflict(RuntimeError):
    """The task is locked by another reviewer or changed concurrently."""


class ReviewValidationError(ValueError):
    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _document_id(path: Path) -> str:
    return f"doc_{hashlib.sha256(path.read_bytes()).hexdigest()[:20]}" if path.is_file() else f"doc_{uuid4().hex[:20]}"


def _engine(database_url: str | None = None) -> Engine:
    url = database_url or config.REVIEW_DATABASE_URL
    with _LOCK:
        engine = _ENGINES.get(url)
        if engine is None:
            if url.startswith("sqlite:///"):
                Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(url, future=True, pool_pre_ping=True)
            _METADATA.create_all(engine)
            _ENGINES[url] = engine
        return engine


def _history(conn, document_id: str) -> list[dict]:
    rows = conn.execute(select(_REVISIONS).where(_REVISIONS.c.document_id == document_id).order_by(_REVISIONS.c.id)).mappings()
    return [dict(row) for row in rows]


def _existing(conn, document_id: str) -> dict:
    """The record of a task this transaction has just written."""
    record = _record(conn, document_id)
    if record is None:
        raise KeyError(document_id)
    return record


def _record(conn, document_id: str) -> dict | None:
    row = conn.execute(select(_TASKS).where(_TASKS.c.document_id == document_id)).mappings().first()
    if row is None:
        return None
    record = dict(row)
    record["history"] = _history(conn, document_id)
    record["locked"] = bool(record["lock_token"] and (record["lock_expires_at"] or 0) > time.time())
    return record


def _revision(conn, document_id: str, action: str, version: int, *, actor: str | None = None,
              note: str | None = None, corrections: dict | None = None,
              validation_issues: list[dict] | None = None) -> None:
    conn.execute(insert(_REVISIONS).values(document_id=document_id, at=_now(), action=action, actor=actor,
                                           note=note, corrections=corrections,
                                           validation_issues=validation_issues, version=version))


def enqueue(result: DocumentResult, reasons: list[str], *, database_url: str | None = None,
            documents_dir: Path | None = None) -> str:
    documents_dir = config.REVIEW_DOCUMENTS_DIR if documents_dir is None else Path(documents_dir)
    source, document_id = Path(result.source), result.document_id or _document_id(Path(result.source))
    original_path: str | None = None
    if source.is_file():
        documents_dir.mkdir(parents=True, exist_ok=True)
        destination = documents_dir / f"{document_id}{source.suffix.lower()}"
        if source.resolve() != destination.resolve() and not destination.exists():
            shutil.copy2(source, destination)
        original_path = str(destination)
    with _engine(database_url).begin() as conn:
        existing = conn.execute(select(_TASKS.c.version, _TASKS.c.original_path).where(_TASKS.c.document_id == document_id)).mappings().first()
        now = _now()
        values = dict(status="pending", updated_at=now, source=result.source, original_path=original_path,
                      doc_type=result.document_type, reasons=reasons, result=result.model_dump(mode="json"),
                      corrections=None, validation_issues=[], lock_owner=None, lock_token=None, lock_expires_at=None)
        if existing is None:
            version, action = 1, "queued"
            conn.execute(insert(_TASKS).values(document_id=document_id, queued_at=now, version=version, **values))
        else:
            version, action = existing["version"] + 1, "requeued"
            values["original_path"] = original_path or existing["original_path"]
            conn.execute(sql_update(_TASKS).where(_TASKS.c.document_id == document_id).values(version=version, **values))
        _revision(conn, document_id, action, version)
    return document_id


def list_pending(*, database_url: str | None = None) -> list[dict]:
    with _engine(database_url).connect() as conn:
        ids: list[str] = list(conn.execute(select(_TASKS.c.document_id).where(
            _TASKS.c.status.in_(("pending", "in_review", "corrected"))).order_by(_TASKS.c.queued_at)).scalars())
        return [record for document_id in ids if (record := _record(conn, document_id)) is not None]


def get(document_id: str, *, database_url: str | None = None) -> dict | None:
    with _engine(database_url).connect() as conn:
        return _record(conn, document_id)


def claim(document_id: str, *, actor: str, lease_seconds: int | None = None,
          database_url: str | None = None) -> dict:
    lease = lease_seconds or config.REVIEW_LOCK_SECONDS
    if lease < 10 or lease > 3600:
        raise ValueError("lease_seconds must be between 10 and 3600")
    engine = _engine(database_url)
    with engine.begin() as conn:
        row = conn.execute(select(_TASKS).where(_TASKS.c.document_id == document_id).with_for_update()).mappings().first()
        if row is None:
            raise KeyError(document_id)
        now = time.time()
        if row["lock_token"] and (row["lock_expires_at"] or 0) > now and row["lock_owner"] != actor:
            raise ReviewConflict(f"task is locked by {row['lock_owner']}")
        token, version = uuid4().hex, row["version"] + 1
        result = conn.execute(sql_update(_TASKS).where(
            (_TASKS.c.document_id == document_id) &
            ((_TASKS.c.lock_token.is_(None)) | (_TASKS.c.lock_expires_at <= now) | (_TASKS.c.lock_owner == actor))
        ).values(status="in_review", lock_owner=actor, lock_token=token, lock_expires_at=now + lease,
                 updated_at=_now(), version=version))
        if result.rowcount != 1:
            raise ReviewConflict("task was claimed concurrently")
        _revision(conn, document_id, "claimed", version, actor=actor)
        return _existing(conn, document_id)


def _require_lock(row: Any, token: str | None) -> None:
    if not token or token != row["lock_token"] or (row["lock_expires_at"] or 0) <= time.time():
        raise ReviewConflict("a current lock_token is required")


def release(document_id: str, *, lock_token: str, actor: str, database_url: str | None = None) -> dict:
    with _engine(database_url).begin() as conn:
        row = conn.execute(select(_TASKS).where(_TASKS.c.document_id == document_id).with_for_update()).mappings().first()
        if row is None:
            raise KeyError(document_id)
        _require_lock(row, lock_token)
        version, status = row["version"] + 1, "corrected" if row["corrections"] else "pending"
        conn.execute(sql_update(_TASKS).where(_TASKS.c.document_id == document_id).values(
            status=status, lock_owner=None, lock_token=None, lock_expires_at=None, updated_at=_now(), version=version))
        _revision(conn, document_id, "released", version, actor=actor)
        return _existing(conn, document_id)


def _merge(base: dict, changes: dict) -> dict:
    merged = dict(base)
    for key, value in changes.items():
        merged[key] = _merge(merged.get(key, {}), value) if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


def _revalidate(row: Any, corrections: dict | None) -> list[dict]:
    from pydantic import ValidationError

    from . import catalog
    from .validate import validate
    saved = DocumentResult.model_validate(row["result"])
    spec = catalog.get_schema(saved.schema_id, saved.schema_version) if saved.schema_id else None
    if spec is None:
        raise ReviewValidationError(f"schema {saved.schema_id!r} is not available")
    try:
        document = spec.model.model_validate(_merge(saved.extracted or {}, corrections or {}))
    except ValidationError as exc:
        issues = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"], "severity": "error"} for e in exc.errors()]
        raise ReviewValidationError("corrections do not match the document schema", issues) from exc
    pages = [page.text for page in saved.layout.pages] if saved.layout else None
    return [i.model_dump(mode="json") for i in validate(document, pages=pages, spec=spec)]


def update(document_id: str, *, status: str, corrections: dict | None = None, actor: str = "reviewer",
           note: str | None = None, lock_token: str | None = None, expected_version: int | None = None,
           database_url: str | None = None) -> dict:
    if status not in _STATUSES:
        raise ValueError(f"invalid review status: {status}")
    with _engine(database_url).begin() as conn:
        row = conn.execute(select(_TASKS).where(_TASKS.c.document_id == document_id).with_for_update()).mappings().first()
        if row is None:
            raise KeyError(document_id)
        _require_lock(row, lock_token)
        if expected_version is not None and expected_version != row["version"]:
            raise ReviewConflict(f"task changed: expected version {expected_version}, current {row['version']}")
        combined = _merge(row["corrections"] or {}, corrections or {})
        issues = _revalidate(row, combined)
        if status == "approved" and any(i["severity"] == "error" for i in issues):
            raise ReviewValidationError("corrected document still has validation errors", issues)
        version, terminal = row["version"] + 1, status in {"approved", "rejected"}
        conn.execute(sql_update(_TASKS).where(_TASKS.c.document_id == document_id).values(
            status=status, corrections=combined, validation_issues=issues, updated_at=_now(), version=version,
            lock_owner=None if terminal else row["lock_owner"], lock_token=None if terminal else row["lock_token"],
            lock_expires_at=None if terminal else row["lock_expires_at"]))
        _revision(conn, document_id, status, version, actor=actor, note=note,
                  corrections=corrections, validation_issues=issues)
        return _existing(conn, document_id)


def revalidate(document_id: str, *, lock_token: str, actor: str = "reviewer",
               database_url: str | None = None) -> dict:
    record = get(document_id, database_url=database_url)
    if record is None:
        raise KeyError(document_id)
    return update(document_id, status="corrected", corrections={}, actor=actor, lock_token=lock_token,
                  expected_version=record["version"], database_url=database_url)


def clear(*, database_url: str | None = None) -> None:
    with _engine(database_url).begin() as conn:
        conn.execute(delete(_REVISIONS))
        conn.execute(delete(_TASKS))


__all__ = ["ReviewConflict", "ReviewValidationError", "claim", "clear", "enqueue", "get",
           "list_pending", "release", "revalidate", "update"]
