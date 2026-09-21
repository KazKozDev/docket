"""Process-wide concurrency limits, shared by every caller in the process.

Batch workers, HTTP jobs and library threads all compete for the same LLM
endpoint and the same CPU. The limits sit at the two expensive calls, not
at the document level, so a batch with 8 workers still sends at most
`DOCKET_LLM_CONCURRENCY` requests to the model and runs at most
`DOCKET_OCR_CONCURRENCY` OCR engines at once.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from . import config

_lock = threading.Lock()
_slots: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_active: dict[str, int] = {}
_peak: dict[str, int] = {}


def _semaphore(name: str, size: int) -> threading.BoundedSemaphore:
    with _lock:
        current = _slots.get(name)
        if current is None or current[0] != size:
            current = (size, threading.BoundedSemaphore(max(1, size)))
            _slots[name] = current
        return current[1]


@contextmanager
def slot(name: str) -> Iterator[None]:
    """Hold one of the `name` slots ("llm" or "ocr") for the duration."""
    size = config.LLM_CONCURRENCY if name == "llm" else config.OCR_CONCURRENCY
    semaphore = _semaphore(name, size)
    with semaphore:
        with _lock:
            _active[name] = _active.get(name, 0) + 1
            _peak[name] = max(_peak.get(name, 0), _active[name])
        try:
            yield
        finally:
            with _lock:
                _active[name] -= 1


def peak(name: str) -> int:
    """Highest number of simultaneous holders seen since the last reset."""
    return _peak.get(name, 0)


def reset_peaks() -> None:
    with _lock:
        _peak.clear()


__all__ = ["peak", "reset_peaks", "slot"]
