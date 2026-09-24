"""Check an extraction docket did not make: `verify()`.

The same deterministic checks `process_document` runs — cited quotes
against the source text, arithmetic, dates, check digits — applied to a
document produced anywhere else (another LLM, a cloud OCR service, a hand
entry). The result is an ordinary `DocumentResult`, so `export_document`
refuses it on the same terms as one docket read itself.
"""
from __future__ import annotations

from pydantic import BaseModel, ValidationError

from . import catalog
from .result import DocumentError, DocumentResult, DocumentStatus, SourceLocation
from .review_reasons import reasons_for
from .validate import validate

NO_SOURCE_TEXT = "no source text given — citations and values were not checked against the document"


def verify(
    document: BaseModel | dict,
    text: str | list[str] | None = None,
    *,
    document_type: str | None = None,
    source: str = "<external>",
) -> DocumentResult:
    """Validate an externally produced document against its source text.

    `document` is a schema instance, or a dict with `document_type` naming
    its schema; citations go in its `field_locations` ({path: {page, quote}}).
    `text` is the document's text — one string, or one string per page so
    citations are checked on the page they name. Without it only the
    document's internal consistency is checked, and the result needs review.
    """
    spec = catalog.get_schema(document_type) if document_type else None
    if document_type and spec is None:
        raise catalog.SchemaError(f"unknown document type: {document_type}")
    if isinstance(document, BaseModel):
        spec = spec or catalog.for_model(type(document)) or catalog.adhoc(type(document))
        instance = document
    else:
        if spec is None:
            raise catalog.SchemaError("document_type is required when the document is a dict")
        try:
            instance = spec.model.model_validate(document)
        except ValidationError as exc:
            return DocumentResult(
                source=source,
                document_id=source,
                status=DocumentStatus.FAILED,
                document_type=spec.schema_id,
                schema_id=spec.schema_id,
                schema_version=spec.version,
                error=DocumentError(code="invalid_document", stage="verify", message=str(exc)),
                review_reasons=[f"verify failed: {exc.error_count()} schema error(s)"],
                needs_review=True,
            )

    pages = text if isinstance(text, list) else None
    raw_text = text if isinstance(text, str) else None
    issues = validate(instance, raw_text, pages=pages, spec=spec)

    extracted = instance.model_dump(mode="json")
    citations = extracted.pop("field_locations", None) or {}
    result = DocumentResult(
        source=source,
        document_id=source,
        status=DocumentStatus.SUCCEEDED,
        document_type=spec.schema_id,
        schema_id=spec.schema_id,
        schema_version=spec.version,
        extracted=extracted,
        field_sources={field: SourceLocation(page=c["page"], quote=c["quote"]) for field, c in citations.items()},
        validation_issues=issues,
    )
    reasons = reasons_for(result)
    # reasons_for reads page completeness from the layout, which an external
    # document does not have; that reason says nothing about it.
    reasons = [r for r in reasons if not r.startswith("incomplete processing")]
    if text is None or not (text if isinstance(text, str) else any(p.strip() for p in text)):
        reasons.append(NO_SOURCE_TEXT)
    return result.model_copy(
        update={
            "needs_review": bool(reasons),
            "review_reasons": reasons,
            "status": DocumentStatus.NEEDS_REVIEW if reasons else DocumentStatus.SUCCEEDED,
        }
    )


__all__ = ["verify"]
