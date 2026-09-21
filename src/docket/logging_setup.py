"""Structured logging for the service.

JSON lines rather than prose, because the consumer is a log aggregator, not
a person scrolling a terminal. Each pipeline run logs one record per stage
with its own latency, so a slow document can be diagnosed from the logs
alone — which is the difference between "it was slow yesterday" and "the
vision re-read took 12 seconds on that scan".

Configured once, from the app entry points. Importing `docket` never
configures logging: a library that reconfigures the root logger on import
is a library that fights whatever host it's embedded in.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager

LOGGER_NAME = "docket"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Anything passed via `extra=` rides along as its own field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def configure(level: str | None = None) -> logging.Logger:
    """Attach a JSON handler to the docket logger. Idempotent."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level or os.getenv("DOCKET_LOG_LEVEL", "INFO").upper())
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def log_stage(logger: logging.Logger, stage: str, **fields):
    """Time a pipeline stage and log how it ended, either way.

    A stage that raises still gets a record — losing the timing of the thing
    that broke is losing the thing you most wanted to see.
    """
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        logger.error(
            f"{stage} failed",
            extra={
                "stage": stage,
                "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": type(exc).__name__,
                **fields,
            },
        )
        raise
    else:
        logger.info(
            f"{stage} done",
            extra={
                "stage": stage,
                "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                **fields,
            },
        )
