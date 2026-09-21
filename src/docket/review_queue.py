"""Durable human-review records with preserved originals and audit history."""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config
from .result import DocumentResult

_LOCK = threading.RLock()
_STATUSES = {"pending", "in_review", "corrected", "approved", "rejected"}


def reasons_for(result: DocumentResult) -> list[str]:
    reasons: list[str] = []
    if result.error is not None:
        reasons.append(f"{result.error.stage} failed: {result.error.message}")
        return reasons
    if not result.complete:
        empty = [p.page_number for p in (result.layout.pages if result.layout else []) if not p.text.strip()]
        reasons.append(f"incomplete processing (no text on page(s) {', '.join(map(str, empty)) or '?'})")
    classification = result.classification
    if classification is not None:
        if classification.confidence < config.MIN_CLASSIFICATION_CONFIDENCE:
            reasons.append(
                f"low classification confidence ({classification.confidence:.2f} "
                f"< {config.MIN_CLASSIFICATION_CONFIDENCE:.2f})"
            )
        if classification.type_name == "unknown":
            reasons.append("unrecognized document type")
    degraded = result.ocr.degraded_pages if result.ocr else []
    if degraded:
        reasons.append(
            f"text on page(s) {', '.join(map(str, degraded))} came from a reading below "
            "the confidence gate — every backend that should have read it better "
            "failed or was unavailable"
        )
    if result.extracted is None:
        reasons.append("extraction failed to produce valid structured output")
    for issue in result.validation_issues:
        if issue.severity == "error":
            reasons.append(f"validation error: {issue.field} — {issue.message}")
    return reasons


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _document_id(path: Path) -> str:
    if path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
        return f"doc_{digest}"
    return f"doc_{uuid4().hex[:20]}"


def _append(event: dict) -> None:
    config.REVIEW_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with config.REVIEW_QUEUE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _records() -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not config.REVIEW_QUEUE_PATH.exists():
        return records
    for line in config.REVIEW_QUEUE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        document_id = event.get("document_id")
        if not document_id:
            # Backward compatibility for queues written by the original demo.
            document_id = f"legacy_{len(records) + 1}"
            event = {
                "event": "created",
                "document_id": document_id,
                "at": event.get("queued_at", _now()),
                "record": {**event, "document_id": document_id, "status": "pending"},
            }
        if event.get("event") == "created":
            records[document_id] = event["record"]
            records[document_id].setdefault("history", []).append(
                {"at": event["at"], "action": "queued"}
            )
        elif document_id in records:
            record = records[document_id]
            record.update(event.get("changes", {}))
            record.setdefault("history", []).append(
                {
                    "at": event["at"],
                    "action": event.get("event", "updated"),
                    "actor": event.get("actor"),
                    "note": event.get("note"),
                }
            )
    return records


def enqueue(result: DocumentResult, reasons: list[str]) -> str:
    source = Path(result.source)
    document_id = result.document_id or _document_id(source)
    original_path: str | None = None
    with _LOCK:
        existing = _records().get(document_id)
        if source.is_file():
            config.REVIEW_DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
            destination = (
                config.REVIEW_DOCUMENTS_DIR / f"{document_id}{source.suffix.lower()}"
            )
            if source.resolve() != destination.resolve() and not destination.exists():
                shutil.copy2(source, destination)
            original_path = str(destination)
        record = {
            "document_id": document_id,
            "queued_at": _now(),
            "updated_at": _now(),
            "status": "pending",
            "source": result.source,
            "original_path": original_path,
            "doc_type": result.document_type,
            "reasons": reasons,
            "result": result.model_dump(mode="json"),
            "corrections": None,
        }
        if existing is None:
            _append(
                {
                    "event": "created",
                    "document_id": document_id,
                    "at": _now(),
                    "record": record,
                }
            )
        else:
            _append(
                {
                    "event": "requeued",
                    "document_id": document_id,
                    "at": _now(),
                    "changes": {
                        **record,
                        "original_path": original_path or existing.get("original_path"),
                    },
                }
            )
    return document_id


def list_pending() -> list[dict]:
    with _LOCK:
        records = _records().values()
        return [
            r
            for r in records
            if r.get("status") in {"pending", "in_review", "corrected"}
        ]


def get(document_id: str) -> dict | None:
    with _LOCK:
        return _records().get(document_id)


def update(
    document_id: str,
    *,
    status: str,
    corrections: dict | None = None,
    actor: str = "reviewer",
    note: str | None = None,
) -> dict:
    if status not in _STATUSES:
        raise ValueError(f"invalid review status: {status}")
    with _LOCK:
        if document_id not in _records():
            raise KeyError(document_id)
        changes = {"status": status, "updated_at": _now()}
        if corrections is not None:
            changes["corrections"] = corrections
        _append(
            {
                "event": status,
                "document_id": document_id,
                "at": _now(),
                "actor": actor,
                "note": note,
                "changes": changes,
            }
        )
        return _records()[document_id]


def clear() -> None:
    with _LOCK:
        config.REVIEW_QUEUE_PATH.unlink(missing_ok=True)
