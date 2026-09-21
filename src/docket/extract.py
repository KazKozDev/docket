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
printed — never adjust a number to make totals reconcile.
{date_instruction}
Page markers are part of the provenance and must be retained in source citations.
Every material value needs a citation: cite top-level fields by their dotted
path, and cite every row of every repeated list per field, with its schema
path — "line_items[0].quantity", "items[0].price", "transactions[0].amount", ...
The quote for a row field is that row's own text on the page. A value with
no verifiable source is a fabrication; cite what you read, and read what you
cite.

JSON Schema:
{schema}

Document:
{text}
"""


_PARTIAL_PROMPT = """Extract every field explicitly present in this document chunk.
Return a JSON object containing only fields supported by this chunk. Do not
invent missing required fields and do not reconcile inconsistent numbers.
Cite every material value in field_locations, including every row of every
repeated list per field ("line_items[0].total", "items[0].price", ...), with
the quote being that row's text.

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
            log.warning(
                "extraction request failed",
                extra={"attempt": attempt, "error_type": type(exc).__name__},
            )
            if attempt > max_retries:
                return None, attempt
            continue
        try:
            return model_cls.model_validate(raw), attempt
        except ValidationError as exc:
            log.warning(
                "extraction schema validation failed",
                extra={
                    "attempt": attempt,
                    "fields": [str(error["loc"]) for error in exc.errors()],
                },
            )
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
        pieces = [
            marker + body[i : i + body_limit] for i in range(0, len(body), body_limit)
        ] or [marker]
        for piece in pieces:
            if current and len(current) + len(piece) > limit:
                chunks.append(current)
                current = ""
            current += piece
    if current:
        chunks.append(current)
    return chunks


def _date_instruction_for(text: str) -> str:
    from .validate import _document_date_convention

    convention = _document_date_convention(text)
    if convention == "dmy":
        return (
            "IMPORTANT: This document uses DMY (Day/Month/Year) date format, judging by its "
            "unambiguous dates or its decimal-comma amounts (e.g. DD/MM/YYYY). Interpret ALL ambiguous numeric dates "
            "(such as 11/02/2019 -> February 11, 2019) strictly as Day/Month/Year. "
            "Never mix conventions within one document."
        )
    if convention == "mdy":
        return (
            "IMPORTANT: This document uses MDY (Month/Day/Year) date format based on "
            "unambiguous dates (e.g. MM/DD/YYYY). Interpret ALL ambiguous numeric dates "
            "strictly as Month/Day/Year. Never mix conventions within one document."
        )
    return (
        "Interpret every ambiguous numeric date under a single consistent convention: "
        "prefer the one under which each date in the document is calendar-valid, and "
        "never mix conventions within one document."
    )


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
        full_text = chunks[0]
        prompt = _EXTRACT_PROMPT.format(
            schema=json.dumps(schema, ensure_ascii=False),
            text=full_text,
            date_instruction=_date_instruction_for(full_text),
        )
        return _validated_call(prompt, full_text, model_cls, max_retries=max_retries)

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
