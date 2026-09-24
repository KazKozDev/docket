"""Many documents through `process_document`: ordered, bounded, resumable.

    from docket import BatchOptions, ProcessOptions, process_batch

    batch = process_batch("invoices/", ProcessOptions(document_type="invoice"),
                          BatchOptions(recursive=True, workers=4, checkpoint="run.jsonl"))
    batch.succeeded, batch.failed, batch.needs_review

- **Sources**: a directory (optionally recursive, filtered by `glob`), a glob
  pattern, one file, or any iterable of paths. Files are discovered lazily
  and only supported file types are taken from directories.
- **Order**: `results` follows the input order whatever order documents
  finish in; `on_result(index, result)` streams them in that order too.
- **Failures**: one document failing never stops the batch unless
  `fail_fast` — it comes back as a failed DocumentResult and in `errors`.
  Configuration errors are raised before the first document is read.
- **Concurrency**: `workers` documents in flight; the LLM and OCR limits in
  `docket.limits` bound the expensive calls across all workers.
- **Memory**: at most `2 × workers` documents are submitted ahead of the one
  being yielded, so a large directory is never listed into memory whole.
  `keep_results=False` drops results after `on_result` has seen them.
- **Resume**: with a `checkpoint` (JSON Lines), every finished result is
  appended as it completes; a rerun reuses the recorded result of any source
  whose content hash is unchanged instead of processing it again.
"""
from __future__ import annotations

import contextvars
import glob as globlib
import os
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import config
from .logging_setup import get_logger
from .ocr import SUPPORTED_SUFFIXES
from .options import ProcessOptions, ResolvedOptions, resolve
from .pipeline import document_id_for, process_document
from .result import DocumentError, DocumentResult, DocumentStatus

log = get_logger()

SourceSpec = str | Path | Iterable[str | Path]
ResultCallback = Callable[[int, DocumentResult], None]


class BatchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recursive: bool = Field(default=False, description="Descend into subdirectories of a directory source.")
    glob: str | None = Field(
        default=None, description="Filename pattern within a directory source, e.g. '*.pdf'."
    )
    workers: int | None = Field(default=None, ge=1, le=64, description="Env: DOCKET_BATCH_WORKERS.")
    fail_fast: bool = Field(default=False, description="Stop submitting documents after the first failure.")
    checkpoint: Path | None = Field(default=None, description="JSON Lines file of finished results, for resume.")
    keep_results: bool = Field(default=True, description="Keep every result in BatchResult.results.")


class BatchError(BaseModel):
    index: int
    source: str
    code: str
    stage: str
    message: str


class BatchMetrics(BaseModel):
    documents_processed: int = 0
    documents_resumed: int = 0
    pages: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_unreported_calls: int = 0
    llm_estimated_tokens: int = 0
    escalated_to_vlm: int = 0
    vlm_pages: int = 0
    document_seconds_mean: float | None = None
    document_seconds_median: float | None = None
    stage_seconds: dict[str, float] = Field(default_factory=dict)


class BatchResult(BaseModel):
    results: list[DocumentResult] = Field(default_factory=list)
    errors: list[BatchError] = Field(default_factory=list)
    total: int = Field(default=0, description="Sources seen, processed or not.")
    succeeded: int = 0
    failed: int = 0
    needs_review: int = 0
    skipped: int = Field(default=0, description="Sources not processed because fail_fast stopped the batch.")
    elapsed_seconds: float = 0.0
    metrics: BatchMetrics = Field(default_factory=BatchMetrics)

    @property
    def stopped_early(self) -> bool:
        return self.skipped > 0


# ---- sources ------------------------------------------------------------------


def _scan(directory: Path, pattern: str, recursive: bool) -> Iterator[Path]:
    """Supported files under a directory, sorted per directory, lazily."""
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return
    subdirs = []
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            subdirs.append(Path(entry.path))
        elif (
            entry.is_file()
            and Path(entry.name).suffix.lower() in SUPPORTED_SUFFIXES
            and Path(entry.name).match(pattern)
        ):
            yield Path(entry.path)
    if recursive:
        for sub in subdirs:
            yield from _scan(sub, pattern, recursive)


def iter_sources(sources: SourceSpec, *, glob: str | None = None, recursive: bool = False) -> Iterator[Path]:
    """Expand a batch source into document paths, in a deterministic order."""
    if isinstance(sources, (str, Path)):
        path = Path(sources)
        if path.is_dir():
            yield from _scan(path, glob or "*", recursive)
        elif globlib.has_magic(str(sources)):
            for match in sorted(globlib.iglob(str(sources), recursive=recursive)):
                if Path(match).is_file():
                    yield Path(match)
        else:
            yield path  # a single file; a missing one fails as a document
        return
    for item in sources:
        yield Path(item)


# ---- checkpoint ---------------------------------------------------------------


class Checkpoint:
    """Append-only JSON Lines of finished results, keyed by source."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._done: dict[str, DocumentResult] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    result = DocumentResult.model_validate_json(line)
                except ValueError:
                    continue  # a line cut short by a crash
                self._done[result.source] = result

    def reusable(self, path: Path) -> DocumentResult | None:
        """The recorded result for this source, if the file is unchanged."""
        recorded = self._done.get(str(path))
        if recorded is None or recorded.status == DocumentStatus.FAILED:
            return None
        try:
            return recorded if document_id_for(path) == recorded.document_id else None
        except OSError:
            return None

    def record(self, result: DocumentResult) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(result.model_dump_json() + "\n")


# ---- running ------------------------------------------------------------------


def _run_one(path: Path, options: ResolvedOptions) -> DocumentResult:
    try:
        return process_document(path, options)
    except Exception as exc:
        log.exception("document crashed", extra={"document": path.name})
        return DocumentResult(
            source=str(path),
            document_id=f"crashed:{path.name}",
            status=DocumentStatus.FAILED,
            needs_review=True,
            review_reasons=[f"internal error: {type(exc).__name__}: {exc}"],
            error=DocumentError(code="internal_error", stage="pipeline", message=f"{type(exc).__name__}: {exc}"),
        )


def iter_batch(
    sources: SourceSpec,
    options: ProcessOptions | ResolvedOptions | None = None,
    batch: BatchOptions | None = None,
) -> Iterator[tuple[int, Path, DocumentResult | None, bool]]:
    """(index, path, result, resumed) in input order. `result` is None for a
    source skipped after a fail-fast stop."""
    batch = batch or BatchOptions()
    resolved = options if isinstance(options, ResolvedOptions) else resolve(options)
    workers = batch.workers or config.BATCH_WORKERS
    checkpoint = Checkpoint(batch.checkpoint) if batch.checkpoint else None
    paths = iter_sources(sources, glob=batch.glob, recursive=batch.recursive)
    window: deque[tuple[int, Path, Future | DocumentResult]] = deque()
    stop = threading.Event()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="docket-batch") as pool:

        def submit(index: int, path: Path) -> None:
            reused = checkpoint.reusable(path) if checkpoint else None
            if reused is not None:
                window.append((index, path, reused))
                return
            # A fresh context per document: LLM usage counters are
            # context-local, so each result counts only its own calls.
            context = contextvars.copy_context()
            window.append((index, path, pool.submit(context.run, _run_one, path, resolved)))

        index = 0
        exhausted = False

        def fill() -> None:
            nonlocal index, exhausted
            while not exhausted and not stop.is_set() and len(window) < 2 * workers:
                path = next(paths, None)
                if path is None:
                    exhausted = True
                    return
                submit(index, path)
                index += 1

        fill()
        while window:
            i, path, pending = window.popleft()
            resumed = isinstance(pending, DocumentResult)
            result: DocumentResult = pending if isinstance(pending, DocumentResult) else pending.result()
            if not resumed and checkpoint is not None:
                checkpoint.record(result)
            if batch.fail_fast and result.status == DocumentStatus.FAILED:
                stop.set()
            yield i, path, result, resumed
            fill()
        # Sources never submitted because of fail-fast.
        if stop.is_set():
            for path in paths:
                yield index, path, None, False
                index += 1


def process_batch(
    sources: SourceSpec,
    options: ProcessOptions | ResolvedOptions | None = None,
    batch: BatchOptions | None = None,
    *,
    on_result: ResultCallback | None = None,
) -> BatchResult:
    """Process many documents; see the module docstring for the contract."""
    batch = batch or BatchOptions()
    resolved = options if isinstance(options, ResolvedOptions) else resolve(options)
    started = time.monotonic()
    out = BatchResult()
    seconds: list[float] = []
    stages: dict[str, float] = {}
    metrics = out.metrics

    for index, path, result, resumed in iter_batch(sources, resolved, batch):
        out.total += 1
        if result is None:
            out.skipped += 1
            continue
        if resumed:
            metrics.documents_resumed += 1
        else:
            metrics.documents_processed += 1
            seconds.append(result.metrics.elapsed_seconds)
            metrics.llm_calls += result.metrics.llm_calls
            metrics.llm_input_tokens += result.metrics.llm_input_tokens
            metrics.llm_output_tokens += result.metrics.llm_output_tokens
            metrics.llm_unreported_calls += result.metrics.llm_unreported_calls
            metrics.llm_estimated_tokens += result.metrics.llm_estimated_tokens
            for stage, value in result.metrics.stage_seconds.items():
                stages[stage] = stages.get(stage, 0.0) + value
        metrics.pages += result.metrics.pages
        metrics.escalated_to_vlm += result.metrics.escalated_to_vlm
        if result.ocr is not None:
            metrics.vlm_pages += sum(1 for p in result.ocr.pages if p.backend == "vlm")
        if result.status == DocumentStatus.FAILED:
            out.failed += 1
            error = result.error
            out.errors.append(
                BatchError(
                    index=index,
                    source=result.source,
                    code=error.code if error else "failed",
                    stage=error.stage if error else "pipeline",
                    message=error.message if error else "; ".join(result.review_reasons),
                )
            )
        elif result.status == DocumentStatus.NEEDS_REVIEW:
            out.needs_review += 1
        else:
            out.succeeded += 1
        if on_result is not None:
            on_result(index, result)
        if batch.keep_results:
            out.results.append(result)

    if seconds:
        metrics.document_seconds_mean = round(statistics.fmean(seconds), 3)
        metrics.document_seconds_median = round(statistics.median(seconds), 3)
    metrics.stage_seconds = {k: round(v, 3) for k, v in stages.items()}
    out.elapsed_seconds = round(time.monotonic() - started, 3)
    return out


__all__ = [
    "BatchError",
    "BatchMetrics",
    "BatchOptions",
    "BatchResult",
    "Checkpoint",
    "iter_batch",
    "iter_sources",
    "process_batch",
]
