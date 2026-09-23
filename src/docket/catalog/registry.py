"""The schema registry: every document type docket can extract, versioned.

A `SchemaSpec` is everything the pipeline needs to know about one document
type: the Pydantic model the LLM fills, what the classifier looks for, which
fields must be cited, extra validation rules, and how older JSON of the same
schema migrates forward. Built-in schemas and custom ones are the same kind
of object in the same registry.

    from docket.catalog import SchemaSpec, register_schema

    register_schema(SchemaSpec(
        schema_id="parking_ticket", version="1.0", display_name="Parking ticket",
        description="Municipal parking fine notice", model=ParkingTicket,
        keywords=keywords("parking ticket", "strafzettel", "avis de contravention"),
    ))

Separate packages register schemas through the `docket.schemas` entry point,
whose target is a SchemaSpec, a list of them, or a callable that registers
(or returns) them.
"""
from __future__ import annotations

import re
import typing
from dataclasses import dataclass, field, replace
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Callable, Iterable, Literal

from pydantic import BaseModel

from ..errors import ConfigurationError
from .common import Citation

if TYPE_CHECKING:
    from ..schemas import ValidationIssue

ENTRY_POINT_GROUP = "docket.schemas"
UNKNOWN = "unknown"
DEFAULT_KEYWORD_WEIGHT = 3.0
_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
_VERSION_RE = re.compile(r"\d+(\.\d+)*")


class SchemaError(ConfigurationError):
    """Invalid registration, unknown schema, or a model docket can't use."""


@dataclass(frozen=True)
class ValidationContext:
    """What a validator may look at besides the document itself."""

    raw_text: str | None = None
    pages: list[str] | None = None
    witness_pages: list[str | None] | None = None
    vlm_unconfirmed: bool = False


Validator = Callable[[BaseModel, ValidationContext], "Iterable[ValidationIssue] | None"]


@dataclass(frozen=True)
class Keyword:
    """A classification cue: a case-insensitive pattern and its weight in the
    rules tier. The document's own name counts 3; supporting phrases 1–2."""

    pattern: re.Pattern
    weight: float = DEFAULT_KEYWORD_WEIGHT

    @property
    def text(self) -> str:
        return self.pattern.pattern


def keywords(*phrases: str, weight: float = DEFAULT_KEYWORD_WEIGHT) -> tuple[Keyword, ...]:
    """Literal phrases matched on word boundaries, one Keyword each."""
    return tuple(
        Keyword(re.compile(rf"\b{re.escape(p)}\b", re.I), weight) for p in phrases
    )


def pattern(regex: str, weight: float = DEFAULT_KEYWORD_WEIGHT) -> Keyword:
    """One Keyword from a regular expression (case-insensitive)."""
    return Keyword(re.compile(regex, re.I), weight)


@dataclass(frozen=True)
class Migration:
    """How JSON of `from_version` becomes `to_version`. `upgrade` rewrites an
    extracted-data dict; without one the step is documentation only and
    `migrate()` refuses it."""

    from_version: str
    to_version: str
    description: str
    upgrade: Callable[[dict], dict] | None = None


SUMMARY_COLUMNS = (
    "document_number",
    "document_date",
    "issuer",
    "recipient",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
)
LINE_ITEM_COLUMNS = (
    "description",
    "sku",
    "quantity",
    "unit_of_measure",
    "unit_price",
    "total",
    "tax_rate_percent",
)


@dataclass(frozen=True)
class LineItems:
    """Where a schema keeps its item-like rows, and which item field feeds
    each line-item CSV column (see LINE_ITEM_COLUMNS)."""

    path: str
    columns: dict[str, str]


@dataclass(frozen=True)
class SchemaSpec:
    schema_id: str
    model: type[BaseModel]
    description: str
    version: str | None = "1.0"
    display_name: str = ""
    status: Literal["stable", "experimental"] = "stable"
    keywords: tuple[Keyword, ...] = ()
    examples: tuple[str, ...] = field(
        default=(),
        repr=False,
        metadata={"doc": "Paraphrased sample sentences; they train the TF-IDF tier."},
    )
    cited_fields: tuple[str, ...] | None = None
    validators: tuple[Validator, ...] = ()
    migrations: tuple[Migration, ...] = ()
    summary: dict[str, str] = field(
        default_factory=dict,
        metadata={"doc": "Summary CSV column (SUMMARY_COLUMNS) -> field path in this schema."},
    )
    line_items: LineItems | None = None
    builtin: bool = False
    registered: bool = True

    @property
    def title(self) -> str:
        return self.display_name or self.schema_id.replace("_", " ").capitalize()

    @property
    def has_citations(self) -> bool:
        return "field_locations" in self.model.model_fields

    @property
    def required_citations(self) -> tuple[str, ...]:
        """Field paths that must carry a citation: `cited_fields`, else every
        required top-level field when the model has citations at all."""
        if not self.has_citations:
            return ()
        if self.cited_fields is not None:
            return self.cited_fields
        return tuple(
            name
            for name, info in self.model.model_fields.items()
            if info.is_required() and name != "field_locations"
        )

    def json_schema(self) -> dict:
        return self.model.model_json_schema()

    @property
    def exporters(self) -> list[str]:
        """Export formats that accept this schema's model."""
        from ..export import list_exporters

        return [e.name for e in list_exporters() if issubclass(self.model, e.accepts)]

    def info(self) -> "SchemaInfo":
        return SchemaInfo(
            schema_id=self.schema_id,
            version=self.version,
            display_name=self.title,
            description=self.description,
            status=self.status,
            model=f"{self.model.__module__}:{self.model.__qualname__}",
            builtin=self.builtin,
            has_citations=self.has_citations,
            cited_fields=list(self.required_citations),
            keywords=[k.text for k in self.keywords],
            validators=[getattr(v, "__qualname__", repr(v)) for v in self.validators],
            exporters=self.exporters,
            summary=dict(self.summary),
            line_items=(
                {"path": self.line_items.path, "columns": dict(self.line_items.columns)}
                if self.line_items
                else None
            ),
            migrations=[
                MigrationInfo(
                    from_version=m.from_version,
                    to_version=m.to_version,
                    description=m.description,
                    automatic=m.upgrade is not None,
                )
                for m in self.migrations
            ],
        )


class MigrationInfo(BaseModel):
    from_version: str
    to_version: str
    description: str
    automatic: bool


class SchemaInfo(BaseModel):
    """Serializable metadata of a registered schema (what the CLI and HTTP API list)."""

    schema_id: str
    version: str | None
    display_name: str
    description: str
    status: str
    model: str
    builtin: bool
    has_citations: bool
    cited_fields: list[str]
    keywords: list[str]
    validators: list[str]
    exporters: list[str]
    summary: dict[str, str]
    line_items: dict | None
    migrations: list[MigrationInfo]


# schema_id -> version -> spec
_REGISTRY: dict[str, dict[str, SchemaSpec]] = {}
_plugins_loaded = False


def _version_key(version: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in (version or "0").split("."))


def _check_path(model: type[BaseModel], path: str) -> bool:
    """Does a dotted field path (list indices allowed) exist in the model?"""
    current: object = model
    for part in path.split("."):
        name = part.split("[", 1)[0]
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            return False
        info = current.model_fields.get(name)
        if info is None:
            return False
        current = _inner_model(info.annotation)
    return True


def _inner_model(annotation: object) -> object:
    """The BaseModel inside Optional[...] / list[...] annotations, if any."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _inner_model(arg)
        if found is not None:
            return found
    return None


def check_model(model: object, *, cited_fields: Iterable[str] | None = None) -> None:
    """Raise SchemaError if docket can't extract into this model."""
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise SchemaError(f"a schema must be a Pydantic BaseModel subclass, got {model!r}")
    try:
        model.model_json_schema()
    except Exception as exc:  # noqa: BLE001 — pydantic raises several types here
        raise SchemaError(
            f"{model.__name__} cannot be described as JSON Schema, which extraction needs: {exc}"
        ) from exc
    citations = model.model_fields.get("field_locations")
    if citations is not None and citations.annotation != dict[str, Citation]:
        raise SchemaError(
            f"{model.__name__}.field_locations must be dict[str, Citation] "
            f"(subclass docket.CitedDocument), got {citations.annotation}"
        )
    unknown = [p for p in (cited_fields or ()) if not _check_path(model, p)]
    if unknown:
        raise SchemaError(f"cited_fields not in {model.__name__}: {unknown}")
    if cited_fields and citations is None:
        raise SchemaError(f"{model.__name__} has cited_fields but no field_locations to cite them in")


def register_schema(spec: SchemaSpec, *, replace_existing: bool = False) -> SchemaSpec:
    """Add a schema version to the registry. Every check that can fail later
    fails here instead."""
    _load_plugins()
    return _register(spec, replace_existing=replace_existing)


def _register(spec: SchemaSpec, *, replace_existing: bool = False) -> SchemaSpec:
    if not _ID_RE.fullmatch(spec.schema_id):
        raise SchemaError(f"schema id {spec.schema_id!r} must be lowercase snake_case")
    if spec.schema_id == UNKNOWN:
        raise SchemaError("'unknown' is reserved")
    if spec.version is None or not _VERSION_RE.fullmatch(spec.version):
        raise SchemaError(f"version {spec.version!r} must look like '1.0'")
    if not spec.description.strip():
        raise SchemaError("a description is required: the LLM classifier reads it")
    check_model(spec.model, cited_fields=spec.cited_fields)
    bad_columns = set(spec.summary) - set(SUMMARY_COLUMNS)
    if bad_columns:
        raise SchemaError(f"unknown summary columns {sorted(bad_columns)}; allowed: {', '.join(SUMMARY_COLUMNS)}")
    missing = [p for p in spec.summary.values() if not _check_path(spec.model, p)]
    if spec.line_items is not None:
        if not _check_path(spec.model, spec.line_items.path):
            missing.append(spec.line_items.path)
        bad = set(spec.line_items.columns) - set(LINE_ITEM_COLUMNS)
        if bad:
            raise SchemaError(f"unknown line-item columns {sorted(bad)}; allowed: {', '.join(LINE_ITEM_COLUMNS)}")
    if missing:
        raise SchemaError(f"summary/line-item paths not in {spec.model.__name__}: {missing}")
    for other in list_schemas(all_versions=True):
        if other.model is spec.model and other.schema_id != spec.schema_id:
            raise SchemaError(f"{spec.model.__name__} is already registered as {other.schema_id!r}")
    versions = _REGISTRY.setdefault(spec.schema_id, {})
    existing = versions.get(spec.version)
    if existing is not None and (existing.builtin or not replace_existing):
        raise SchemaError(f"schema {spec.schema_id!r} version {spec.version} is already registered")
    spec = replace(spec, registered=True, display_name=spec.title)
    versions[spec.version] = spec
    return spec


def unregister_schema(schema_id: str, version: str | None = None) -> None:
    """Remove a custom schema (every version, or just one). Built-ins stay."""
    versions = _REGISTRY.get(schema_id)
    if not versions:
        raise SchemaError(f"unknown schema {schema_id!r}")
    targets = [version] if version else list(versions)
    for v in targets:
        spec = versions.get(v)
        if spec is None:
            raise SchemaError(f"schema {schema_id!r} has no version {v}")
        if spec.builtin:
            raise SchemaError(f"{schema_id!r} is built in and cannot be removed")
        del versions[v]
    if not versions:
        del _REGISTRY[schema_id]


def add_validator(schema_id: str, validator: Validator) -> None:
    """Attach an extra rule to every registered version of a schema."""
    _load_plugins()
    versions = _REGISTRY.get(schema_id)
    if not versions:
        raise SchemaError(f"unknown schema {schema_id!r}")
    for version, spec in versions.items():
        versions[version] = replace(spec, validators=spec.validators + (validator,))


def _load_plugins() -> None:
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        target = ep.load()
        produced = target() if callable(target) and not isinstance(target, SchemaSpec) else target
        if isinstance(produced, SchemaSpec):
            produced = [produced]
        for spec in produced or ():
            if not isinstance(spec, SchemaSpec):
                raise SchemaError(f"entry point {ep.name!r} produced {spec!r}, not a SchemaSpec")
            existing = _REGISTRY.get(spec.schema_id, {}).get(spec.version)
            if existing is None or existing.model is not spec.model:
                _register(spec)


def list_schemas(*, all_versions: bool = False) -> list[SchemaSpec]:
    """Registered schemas, latest version of each (or every version)."""
    _load_plugins()
    out = []
    for versions in _REGISTRY.values():
        ordered = sorted(versions.values(), key=lambda s: _version_key(s.version))
        out.extend(ordered if all_versions else ordered[-1:])
    return out


def get_schema(schema_id: str, version: str | None = None) -> SchemaSpec | None:
    """A registered schema; the latest version unless one is named."""
    _load_plugins()
    versions = _REGISTRY.get(str(schema_id))
    if not versions:
        return None
    if version is not None:
        return versions.get(version)
    return max(versions.values(), key=lambda s: _version_key(s.version))


def require_schema(schema_id: str, version: str | None = None) -> SchemaSpec:
    spec = get_schema(schema_id, version)
    if spec is not None:
        return spec
    if get_schema(schema_id) is not None:
        known = ", ".join(sorted(_REGISTRY[schema_id], key=_version_key))
        raise SchemaError(f"schema {schema_id!r} has no version {version}; registered: {known}")
    known = ", ".join(s.schema_id for s in list_schemas())
    raise SchemaError(f"unknown schema {schema_id!r}; known: {known}")


def custom_schemas() -> list[SchemaSpec]:
    return [s for s in list_schemas() if not s.builtin]


def for_model(model: type[BaseModel]) -> SchemaSpec | None:
    for spec in list_schemas(all_versions=True):
        if spec.model is model:
            return spec
    return None


def adhoc(model: type[BaseModel]) -> SchemaSpec:
    """A spec for a model passed straight to the pipeline without registering
    it. Its id is the model's import path; it has no version."""
    check_model(model)
    return SchemaSpec(
        schema_id=f"{model.__module__}:{model.__qualname__}",
        model=model,
        description=(model.__doc__ or model.__name__).strip().splitlines()[0],
        version=None,
        display_name=model.__name__,
        registered=False,
    )


def load_schema(spec: str) -> type[BaseModel]:
    """Import a Pydantic model from `package.module:ClassName`."""
    import importlib

    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise SchemaError(f"schema {spec!r} must look like 'package.module:ClassName'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SchemaError(f"cannot import {module_name!r} for schema {spec!r}: {exc}") from exc
    target: object = module
    for part in attr.split("."):
        target = getattr(target, part, None)
        if target is None:
            raise SchemaError(f"{module_name!r} has no attribute {attr!r}")
    if not (isinstance(target, type) and issubclass(target, BaseModel)):
        raise SchemaError(f"{spec!r} is not a Pydantic BaseModel subclass")
    return target


def resolve(
    *,
    schema_id: str | None = None,
    model: type[BaseModel] | None = None,
    version: str | None = None,
) -> SchemaSpec | None:
    """The schema a caller fixed up front, or None to classify.

    A registered id (optionally a version), a model (registered or not), or
    both — in which case they must agree.
    """
    if schema_id is None and model is None:
        if version is not None:
            raise SchemaError("a schema version needs a document type or schema to apply to")
        return None
    if schema_id is not None:
        spec = require_schema(schema_id, version)
        if model is not None and spec.model is not model:
            raise SchemaError(
                f"document type {schema_id!r} uses {spec.model.__name__}, not {model.__name__}"
            )
        return spec
    spec = for_model(model) or adhoc(model)
    if version is not None and spec.version != version:
        raise SchemaError(f"{model.__name__} is version {spec.version}, not {version}")
    return spec


def parse_type(value: object) -> str:
    """Map a classifier's answer to a registered schema id or 'unknown'."""
    text = str(value or "").strip().lower()
    return text if text != UNKNOWN and get_schema(text) is not None else UNKNOWN


def migrate(schema_id: str, data: dict, from_version: str, to_version: str | None = None) -> dict:
    """Upgrade extracted data of an older schema version, step by step."""
    target = require_schema(schema_id, to_version)
    steps = {m.from_version: m for m in target.migrations}
    current, out = from_version, dict(data)
    while current != target.version:
        step = steps.get(current)
        if step is None:
            raise SchemaError(f"no migration for {schema_id!r} from version {current} to {target.version}")
        if step.upgrade is None:
            raise SchemaError(
                f"{schema_id!r} {step.from_version} → {step.to_version} has no automatic upgrade: {step.description}"
            )
        out = step.upgrade(out)
        current = step.to_version
    return out


__all__ = [
    "ENTRY_POINT_GROUP",
    "Keyword",
    "LINE_ITEM_COLUMNS",
    "LineItems",
    "Migration",
    "MigrationInfo",
    "SchemaError",
    "SchemaInfo",
    "SchemaSpec",
    "SUMMARY_COLUMNS",
    "UNKNOWN",
    "ValidationContext",
    "Validator",
    "add_validator",
    "adhoc",
    "check_model",
    "custom_schemas",
    "for_model",
    "get_schema",
    "keywords",
    "list_schemas",
    "load_schema",
    "migrate",
    "parse_type",
    "pattern",
    "register_schema",
    "require_schema",
    "resolve",
    "unregister_schema",
]
