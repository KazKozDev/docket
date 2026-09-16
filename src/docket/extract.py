"""Schema-constrained extraction with page-aware handling for long documents."""
from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from . import config
from .llm_client import chat_json
from .logging_setup import get_logger

log = get_logger()

_EXTRACT_PROMPT = """Extract structured data from the complete document below.
Respond with one JSON object matching the JSON Schema exactly. Copy values;
do not repair contradictions in the source. Copy every digit exactly as
printed — never adjust a number to make totals reconcile. Interpret every
ambiguous numeric date under a single consistent convention: prefer the one
under which each date in the document is calendar-valid, and never mix
conventions within one document. Page markers are part of the
provenance and must be retained in source citations.

JSON Schema:
{schema}

Document:
{text}
"""

_PARTIAL_PROMPT = """Extract every field explicitly present in this document chunk.
Return a JSON object containing only fields supported by this chunk. Do not
invent missing required fields and do not reconcile inconsistent numbers.

Target JSON Schema:
{schema}

Document chunk:
{text}
"""

_MERGE_PROMPT = """Merge the page-level candidates into one object matching the
JSON Schema exactly. Prefer values with direct source citations. Preserve
contradictions verbatim; never recalculate or silently correct a value.
The source chunks follow each candidate — verify every merged value
against them before accepting it.

JSON Schema:
{schema}

Page-level candidates with their source chunks:
{candidates}
"""

_RETRY_PROMPT = """Your previous JSON did not validate against the schema.

Validation error:
{error}

Previous output:
{previous}

Source document used for this attempt:
{text}

Fix only the validation problem. Respond with one JSON object matching this schema:
{schema}
"""


def _validated_call(
    prompt: str,
    source_text: str,
    model_cls: type[BaseModel],
    *,
    max_retries: int,
) -> tuple[BaseModel | None, int]:
    schema = model_cls.model_json_schema()
    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        try:
            raw = chat_json(prompt, schema=schema)
        except Exception as exc:
            log.warning("extraction request failed", extra={"attempt": attempt, "error_type": type(exc).__name__})
            if attempt > max_retries:
                return None, attempt
            continue
        try:
            return model_cls.model_validate(raw), attempt
        except ValidationError as exc:
            log.warning("extraction schema validation failed", extra={"attempt": attempt, "fields": [str(error['loc']) for error in exc.errors()]})
            if attempt > max_retries:
                return None, attempt
            prompt = _RETRY_PROMPT.format(
                error=str(exc),
                previous=json.dumps(raw, ensure_ascii=False),
                text=source_text,
                schema=json.dumps(schema, ensure_ascii=False),
            )
    return None, attempt


def _page_chunks(pages: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for page_number, page in enumerate(pages, start=1):
        marker = f"\n[PAGE {page_number}]\n"
        body_limit = max(1, limit - len(marker))
        body = page.strip()
        pieces = [marker + body[i : i + body_limit] for i in range(0, len(body), body_limit)] or [marker]
        for piece in pieces:
            if current and len(current) + len(piece) > limit:
                chunks.append(current)
                current = ""
            current += piece
    if current:
        chunks.append(current)
    return chunks


def extract_pages(
    pages: list[str],
    model_cls: type[BaseModel],
    *,
    max_retries: int | None = None,
) -> tuple[BaseModel | None, int]:
    """Extract all pages, chunking and merging when the source exceeds context."""
    max_retries = config.MAX_EXTRACT_RETRIES if max_retries is None else max_retries
    schema = model_cls.model_json_schema()
    chunks = _page_chunks(pages, max(1000, config.EXTRACT_CHUNK_CHARS)) or [""]

    if len(chunks) == 1:
        prompt = _EXTRACT_PROMPT.format(
            schema=json.dumps(schema, ensure_ascii=False), text=chunks[0]
        )
        return _validated_call(prompt, chunks[0], model_cls, max_retries=max_retries)

    candidates: list[dict] = []
    attempts = 0
    for chunk in chunks:
        attempts += 1
        try:
            candidate = chat_json(
                _PARTIAL_PROMPT.format(
                    schema=json.dumps(schema, ensure_ascii=False), text=chunk
                ),
                schema=schema,
            )
        except Exception:
            candidate = {"_chunk_error": True}
        candidates.append({"candidate": candidate, "source": chunk})

    candidate_text = json.dumps(candidates, ensure_ascii=False)
    prompt = _MERGE_PROMPT.format(
        schema=json.dumps(schema, ensure_ascii=False), candidates=candidate_text
    )
    instance, merge_attempts = _validated_call(
        prompt, candidate_text, model_cls, max_retries=max_retries
    )
    return instance, attempts + merge_attempts


def extract(
    text: str,
    model_cls: type[BaseModel],
    *,
    max_retries: int | None = None,
) -> tuple[BaseModel | None, int]:
    """Backward-compatible single-text entry point with no silent truncation."""
    return extract_pages([text], model_cls, max_retries=max_retries)
