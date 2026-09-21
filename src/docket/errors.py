"""Exception base classes shared across docket.

`ConfigurationError` is every mistake that can be found before a document is
read — an unknown OCR backend or language, an unavailable engine named
explicitly, an unknown document type or schema. `process_document` raises
these up front; the CLI maps them to exit code 3 and the HTTP API to 422.
"""
from __future__ import annotations


class ConfigurationError(ValueError):
    """Invalid options or environment, detected before processing starts."""


__all__ = ["ConfigurationError"]
