"""Settings: built-in defaults < config file < environment.

Explicit arguments (`ProcessOptions`, `BatchOptions`, CLI flags, HTTP form
fields) override all three. The config file is TOML:

    # docket.toml
    [ocr]
    backend = "paddle"
    fallbacks = ["tesseract", "vlm"]
    languages = ["en", "de"]

    [batch]
    workers = 8

It is the file named by `docket --config PATH` / `docket-api --config PATH`,
else by `DOCKET_CONFIG`. Relative paths in it are relative to the file.
Every setting has an environment variable (`DOCKET_OCR_BACKEND`, ...;
`docket config show` lists them all).

Importing docket reads only the environment and `DOCKET_CONFIG`: a library
must not pick up files from whatever directory its host happens to run in.
The applications (the CLI, the HTTP service, the demo) call
`configure_app()`, which also reads `.env` from the working directory into
the environment and falls back to `./docket.toml`.

Values are read once, at import (and again by `configure()`), into the
module attributes below, which the rest of the package reads at call time.
An invalid value never raises at import: the default is kept, the problem is
recorded, and `check()` raises one `ConfigurationError` listing every
problem. The CLI, the HTTP server and `process_document()` call `check()`
before touching a document.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv

from .errors import ConfigurationError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Setting:
    name: str  # module attribute
    key: str  # "section.key" in the config file
    env: str
    kind: str  # str, secret, int, float, bool, list, path, choice
    default: Any
    help: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    optional: bool = False  # None ("unset") is a valid value
    lower: bool = False


def _cpu_half() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


SETTINGS: tuple[Setting, ...] = (
    # ---- LLM -----------------------------------------------------------------------
    Setting("LLM_PROVIDER", "llm.provider", "DOCKET_LLM_PROVIDER", "choice", "ollama",
            "'ollama' (local or cloud Ollama) or 'openai' (any OpenAI-compatible Chat Completions API: "
            "Mistral, OpenAI, Azure OpenAI, vLLM, LM Studio).", choices=("ollama", "openai"), lower=True),
    Setting("OLLAMA_HOST", "llm.ollama_host", "OLLAMA_HOST", "str", "http://localhost:11434",
            "Ollama server URL."),
    Setting("LLM_BASE_URL", "llm.base_url", "DOCKET_LLM_BASE_URL", "str", "https://api.openai.com/v1",
            "Base URL of the OpenAI-compatible API."),
    Setting("LLM_API_KEY", "llm.api_key", "DOCKET_LLM_API_KEY", "secret", None,
            "API key for the OpenAI-compatible API.", optional=True),
    Setting("TEXT_MODEL", "llm.text_model", "DOCKET_TEXT_MODEL", "str", "deepseek-v4.1-flash:cloud",
            "Model for classification and extraction."),
    Setting("VISION_MODEL", "llm.vision_model", "DOCKET_VISION_MODEL", "str", "deepseek-v4.1-flash:cloud",
            "Model that reads page images (the vlm OCR backend)."),
    Setting("MAX_EXTRACT_RETRIES", "llm.max_extract_retries", "DOCKET_MAX_EXTRACT_RETRIES", "int", 2,
            "Extra attempts when the model's output fails the schema.", minimum=0, maximum=10),
    Setting("EXTRACT_CHUNK_CHARS", "llm.extract_chunk_chars", "DOCKET_EXTRACT_CHUNK_CHARS", "int", 12000,
            "Longest text sent in one extraction call; longer documents are chunked.", minimum=1000),
    # A full-page scan through a local vision model is slow: 40 s+ is normal on
    # a laptop, a dense form can take minutes. Too low a timeout turns a slow
    # document into a failed one.
    Setting("VISION_TIMEOUT_S", "llm.vision_timeout_s", "DOCKET_VISION_TIMEOUT_S", "float", 300.0,
            "Timeout for one vision-model page, seconds.", minimum=1),
    Setting("LLM_HOSTED_TIMEOUT_S", "llm.hosted_timeout_s", "DOCKET_LLM_HOSTED_TIMEOUT_S", "float", 60.0,
            "Timeout for one request to a hosted model (Ollama cloud, a remote OpenAI-compatible API); "
            "local models keep their longer timeouts.", minimum=1),
    Setting("LLM_RETRIES", "llm.retries", "DOCKET_LLM_RETRIES", "int", 2,
            "Retries after 429/5xx or a connection error, and after a timeout for hosted models.",
            minimum=0, maximum=10),
    # Reasoning models spend output tokens deliberating before a transcription
    # that needs none: one invoice with deepseek-v4.1-flash:cloud took 11.9 s /
    # 661 tokens with thinking, 5.1 s / 171 tokens without, same answer.
    Setting("ENABLE_THINKING", "llm.enable_thinking", "DOCKET_ENABLE_THINKING", "bool", False,
            "Let reasoning models think before answering."),
    Setting("LLM_CONCURRENCY", "llm.concurrency", "DOCKET_LLM_CONCURRENCY", "int", 4,
            "Process-wide limit on simultaneous LLM requests.", minimum=1, maximum=256),
    # ---- OCR -----------------------------------------------------------------------
    Setting("OCR_BACKEND", "ocr.backend", "DOCKET_OCR_BACKEND", "str", "auto",
            "Primary OCR backend: tesseract, paddle, docling, auto (first installed) or a plugin name.", lower=True),
    Setting("OCR_FALLBACKS", "ocr.fallbacks", "DOCKET_OCR_FALLBACKS", "list", ["vlm"],
            "Backends tried in order when a page's reading is rejected (env: comma-separated).", lower=True),
    Setting("OCR_LANGUAGES", "ocr.languages", "DOCKET_OCR_LANGUAGES", "list", ["en"],
            "ISO 639-1 language codes; each extra language slows Tesseract.", lower=True),
    Setting("OCR_MIN_CONFIDENCE", "ocr.min_confidence", "DOCKET_OCR_MIN_CONFIDENCE", "float", 0.60,
            "A page reading is accepted at this confidence (0..1).", minimum=0, maximum=1),
    Setting("OCR_DETECT_ROTATION", "ocr.detect_rotation", "DOCKET_OCR_DETECT_ROTATION", "bool", True,
            "Detect and undo page rotation on scans (Tesseract OSD)."),
    Setting("OCR_DESKEW", "ocr.deskew", "DOCKET_OCR_DESKEW", "bool", True,
            "Correct fine scan skew before raster OCR."),
    Setting("OCR_DPI", "ocr.dpi", "DOCKET_OCR_DPI", "int", 200,
            "Resolution PDF pages are rendered at for OCR.", minimum=72, maximum=600),
    Setting("TESSERACT_PSM", "ocr.tesseract_psm", "DOCKET_TESSERACT_PSM", "int", 3,
            "Tesseract page-segmentation mode (3 automatic; 6 can suit dense tables).", minimum=0, maximum=13),
    Setting("OCR_CONCURRENCY", "ocr.concurrency", "DOCKET_OCR_CONCURRENCY", "int", _cpu_half(),
            "Process-wide limit on simultaneously running OCR engines (default: half the CPUs).",
            minimum=1, maximum=256),
    # A cheap text model judges OCR text no deterministic signal can (a contract
    # has no arithmetic to fail); ~1.3 s, OCR-derived text only.
    Setting("OCR_QUALITY_CHECK", "ocr.quality_check", "DOCKET_OCR_QUALITY_CHECK", "bool", True,
            "Ask a text model whether OCR text is garbled."),
    Setting("OCR_QUALITY_MIN_CONFIDENCE", "ocr.quality_min_confidence", "DOCKET_OCR_QUALITY_MIN_CONFIDENCE",
            "float", 0.7, "Confidence the quality check needs to call text garbled.", minimum=0, maximum=1),
    Setting("PADDLE_DEVICE", "paddle.device", "DOCKET_PADDLE_DEVICE", "str", "cpu",
            "PaddleOCR device: cpu, gpu or gpu:N.", lower=True),
    Setting("PADDLE_MODEL", "paddle.model", "DOCKET_PADDLE_MODEL", "choice", "mobile",
            "PaddleOCR model size.", choices=("mobile", "medium"), lower=True),
    Setting("PADDLE_TABLES", "paddle.tables", "DOCKET_PADDLE_TABLES", "bool", False,
            "Run PaddleOCR's table-structure pipeline."),
    Setting("DOCLING_TABLE_MODE", "docling.table_mode", "DOCKET_DOCLING_TABLE_MODE", "choice", "accurate",
            "TableFormer speed/quality mode.", choices=("fast", "accurate"), lower=True),
    Setting("DOCLING_CELL_MATCHING", "docling.cell_matching", "DOCKET_DOCLING_CELL_MATCHING", "bool", True,
            "Match TableFormer cells back to recognized document text."),
    # ---- layout --------------------------------------------------------------------
    Setting("INCLUDE_LAYOUT", "layout.include", "DOCKET_INCLUDE_LAYOUT", "bool", True,
            "Keep page layouts (words, lines, tables with coordinates) in results."),
    Setting("LAYOUT_MARKERS", "layout.markers", "DOCKET_LAYOUT_MARKERS", "bool", True,
            "Mark tables and columns ([TABLE n], [COLUMN n]) in the page text the LLM reads."),
    # ---- classification and review -------------------------------------------------
    Setting("TFIDF_CONFIDENCE_FLOOR", "classify.tfidf_confidence_floor", "DOCKET_TFIDF_CONFIDENCE_FLOOR",
            "float", 0.65, "Confidence TF-IDF needs to answer instead of the LLM.", minimum=0, maximum=1),
    Setting("MIN_CLASSIFICATION_CONFIDENCE", "classify.min_confidence", "DOCKET_MIN_CONFIDENCE", "float", 0.55,
            "Below this classification confidence a document goes to review.", minimum=0, maximum=1),
    Setting("MIN_SOURCE_CONFIDENCE", "review.min_source_confidence", "DOCKET_MIN_SOURCE_CONFIDENCE", "float", 0.8,
            "A key field read from OCR words below this confidence sends the document to review.",
            minimum=0, maximum=1),
    Setting("REVIEW_QUEUE_ENABLED", "review.enabled", "DOCKET_REVIEW_QUEUE_ENABLED", "bool", False,
            "Write flagged documents to the review queue (opt-in)."),
    Setting("REVIEW_DATABASE_URL", "review.database_url", "DOCKET_REVIEW_DATABASE_URL", "str",
            "sqlite:///data/review.db", "SQLAlchemy URL for SQLite or PostgreSQL review storage."),
    Setting("REVIEW_LOCK_SECONDS", "review.lock_seconds", "DOCKET_REVIEW_LOCK_SECONDS", "int", 300,
            "Review task lease duration before another reviewer can claim it.", minimum=10, maximum=3600),
    Setting("REVIEW_DOCUMENTS_DIR", "review.documents", "DOCKET_REVIEW_DOCUMENTS", "path",
            Path("data/review_documents"), "Where copies of flagged documents are kept."),
    # ---- input, batches, HTTP --------------------------------------------------------
    Setting("MAX_FILE_BYTES", "input.max_file_bytes", "DOCKET_MAX_FILE_BYTES", "int", 20 * 1024 * 1024,
            "Largest accepted file.", minimum=1),
    Setting("MAX_IMAGE_PIXELS", "input.max_image_pixels", "DOCKET_MAX_IMAGE_PIXELS", "int", 50_000_000,
            "Largest decoded image or rendered PDF page in pixels.", minimum=1),
    Setting("MAX_PDF_PAGES", "input.max_pdf_pages", "DOCKET_MAX_PDF_PAGES", "int", 100,
            "Most pages read from one PDF.", minimum=1),
    Setting("BATCH_WORKERS", "batch.workers", "DOCKET_BATCH_WORKERS", "int", 4,
            "Documents in flight per batch; beyond the LLM limit they only help while others are in OCR.",
            minimum=1, maximum=256),
    Setting("MAX_BATCH_FILES", "api.max_batch_files", "DOCKET_MAX_BATCH_FILES", "int", 100,
            "Most files in one HTTP job.", minimum=1),
    Setting("MAX_BATCH_BYTES", "api.max_batch_bytes", "DOCKET_MAX_BATCH_BYTES", "int", 200 * 1024 * 1024,
            "Largest total upload of one HTTP job.", minimum=1),
    Setting("MAX_CONCURRENT_JOBS", "api.max_concurrent_jobs", "DOCKET_MAX_CONCURRENT_JOBS", "int", 2,
            "HTTP jobs processed at once; the rest wait.", minimum=1, maximum=64),
    Setting("API_KEY", "api.key", "DOCKET_API_KEY", "secret", None,
            "Bearer token the HTTP API requires when set.", optional=True),
    Setting("API_CORS_ORIGINS", "api.cors_origins", "DOCKET_API_CORS_ORIGINS", "list",
            ["http://localhost:3000", "http://127.0.0.1:3000"], "Browser origins allowed to call the HTTP API."),
    Setting("JOBS_DIR", "api.jobs_dir", "DOCKET_JOBS_DIR", "path", Path("data/jobs"),
            "One directory per HTTP job: metadata, results, uploads until done."),
    # ---- e-invoices ------------------------------------------------------------------
    Setting("EINVOICE_RESOURCES", "einvoice.resources", "DOCKET_EINVOICE_RESOURCES", "path", None,
            "Your own copy of the validation artifacts (default: the one in the package).", optional=True),
    Setting("EINVOICE_DOWNLOADS", "einvoice.downloads", "DOCKET_EINVOICE_DOWNLOADS", "path", None,
            "Where `docket einvoice fetch` puts the artifacts Docket does not ship "
            "(default: ~/.cache/docket/einvoice).", optional=True),
    # ---- eval and tracing -----------------------------------------------------------------
    # Illustrative only: what this run's LLM calls would cost on a small hosted
    # model. A local Ollama run costs nothing; eval reports show the tradeoff.
    Setting("CLOUD_EQUIVALENT_USD_PER_1M_TOKENS", "eval.cloud_cost_per_1m_tokens",
            "DOCKET_CLOUD_COST_PER_1M_TOKENS", "float", 0.20,
            "Blended $/1M tokens used for the illustrative cost in eval reports.", minimum=0),
    Setting("LANGFUSE_PUBLIC_KEY", "tracing.langfuse_public_key", "LANGFUSE_PUBLIC_KEY", "secret", None,
            "Langfuse tracing (optional).", optional=True),
    Setting("LANGFUSE_SECRET_KEY", "tracing.langfuse_secret_key", "LANGFUSE_SECRET_KEY", "secret", None,
            "Langfuse tracing (optional).", optional=True),
    Setting("LANGFUSE_HOST", "tracing.langfuse_host", "LANGFUSE_HOST", "str", "https://cloud.langfuse.com",
            "Langfuse server."),
)

# Below this many characters per PDF page the page is treated as a scan.
MIN_CHARS_PER_PAGE = 20


def _parse(setting: Setting, raw: Any, from_env: bool, base: Path | None) -> Any:
    """Convert one raw value (an env string or a TOML value). Raises ValueError."""
    kind = setting.kind
    if setting.optional and (raw is None or (isinstance(raw, str) and raw.strip() == "")):
        return None
    if kind in ("str", "secret", "choice"):
        if not isinstance(raw, str):
            raise ValueError("expected a string")
        value = raw.strip()
        if setting.lower:
            value = value.lower()
        if kind == "choice" and value not in setting.choices:
            raise ValueError(f"expected one of {', '.join(setting.choices)}")
        if not value and not setting.optional:
            raise ValueError("must not be empty")
        if setting.name == "LLM_BASE_URL":
            value = value.rstrip("/")
        return value
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if from_env and raw.strip().lower() in _TRUE | _FALSE:
            return raw.strip().lower() in _TRUE
        raise ValueError("expected true or false")
    if kind in ("int", "float"):
        if from_env:
            try:
                value = int(raw.strip()) if kind == "int" else float(raw.strip())
            except ValueError:
                raise ValueError(f"expected {'an integer' if kind == 'int' else 'a number'}") from None
        elif isinstance(raw, bool) or not isinstance(raw, (int, float)) or (kind == "int" and isinstance(raw, float)):
            raise ValueError(f"expected {'an integer' if kind == 'int' else 'a number'}")
        else:
            value = float(raw) if kind == "float" else raw
        if setting.minimum is not None and value < setting.minimum:
            raise ValueError(f"must be at least {setting.minimum:g}")
        if setting.maximum is not None and value > setting.maximum:
            raise ValueError(f"must be at most {setting.maximum:g}")
        return value
    if kind == "list":
        if isinstance(raw, str):
            items = raw.split(",")
        elif isinstance(raw, list) and all(isinstance(i, str) for i in raw):
            items = raw
        else:
            raise ValueError("expected a list of strings")
        return [i.strip().lower() if setting.lower else i.strip() for i in items if i.strip()]
    if kind == "path":
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("expected a path")
        path = Path(raw.strip()).expanduser()
        return base / path if base is not None and not path.is_absolute() else path
    raise AssertionError(kind)


@dataclass
class Loaded:
    values: dict[str, Any]
    sources: dict[str, str]
    errors: list[str]
    file: Path | None


def _find_file(path: str | Path | None, environ: Mapping[str, str],
               discover: bool) -> tuple[Path | None, str | None]:
    if path is not None:
        return Path(path), "--config"
    if environ.get("DOCKET_CONFIG"):
        return Path(environ["DOCKET_CONFIG"]), "DOCKET_CONFIG"
    default = Path("docket.toml")
    return (default, None) if discover and default.is_file() else (None, None)


def load(path: str | Path | None = None, environ: Mapping[str, str] | None = None, *,
         discover: bool = False) -> Loaded:
    """Read defaults, the config file and the environment, without applying
    them. `discover` also falls back to ./docket.toml (applications only)."""
    environ = os.environ if environ is None else environ
    values = {s.name: s.default for s in SETTINGS}
    sources = {s.name: "default" for s in SETTINGS}
    errors: list[str] = []

    file, named_by = _find_file(path, environ, discover)
    if file is not None:
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            errors.append(f"config file {file} (from {named_by}) does not exist")
            data = {}
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"config file {file}: {exc}")
            data = {}
        by_key = {s.key: s for s in SETTINGS}
        for section, table in data.items():
            if not isinstance(table, dict):
                errors.append(f"{file}: '{section}' must be a [section] table")
                continue
            for key, raw in table.items():
                setting = by_key.get(f"{section}.{key}")
                if setting is None:
                    errors.append(f"{file}: unknown setting [{section}] {key}")
                    continue
                try:
                    values[setting.name] = _parse(setting, raw, False, file.parent)
                    sources[setting.name] = f"file {file}"
                except ValueError as exc:
                    errors.append(f"{file}: [{section}] {key} = {raw!r}: {exc}")

    for setting in SETTINGS:
        raw = environ.get(setting.env)
        if raw is None:
            continue
        try:
            values[setting.name] = _parse(setting, raw, True, None)
            sources[setting.name] = f"env {setting.env}"
        except ValueError as exc:
            errors.append(f"{setting.env}={raw!r}: {exc}")

    return Loaded(values=values, sources=sources, errors=errors, file=file)


def _semantic_errors() -> list[str]:
    """Checks beyond one value's type, on the current values. Run by check(),
    not at import: the language table lives in docket.ocr, which imports this
    module."""
    from .ocr.languages import UnknownLanguage, parse_languages

    errors = []
    try:
        parse_languages(globals()["OCR_LANGUAGES"])
    except UnknownLanguage as exc:
        errors.append(f"ocr.languages ({SOURCES.get('OCR_LANGUAGES', 'default')}): {exc}")
    device = globals()["PADDLE_DEVICE"]
    if not (device in ("cpu", "gpu") or (device.startswith("gpu:") and device[4:].isdigit())):
        errors.append(f"paddle.device ({SOURCES.get('PADDLE_DEVICE', 'default')}) = {device!r}: "
                      "expected cpu, gpu or gpu:N")
    return errors


def _apply(loaded: Loaded) -> None:
    globals().update(loaded.values)
    globals().update(SOURCES=dict(loaded.sources), ERRORS=list(loaded.errors), CONFIG_FILE=loaded.file)


def configure(path: str | Path | None = None, environ: Mapping[str, str] | None = None, *,
              discover: bool = False) -> None:
    """(Re)load the settings, e.g. from `--config PATH`. Problems are kept for check()."""
    _apply(load(path, environ, discover=discover))


def configure_app(path: str | Path | None = None) -> None:
    """Settings for an application entry point: `.env` from the working
    directory into the environment (never overriding it), then `path`, else
    DOCKET_CONFIG, else ./docket.toml."""
    load_dotenv(Path(".env"))
    configure(path, discover=True)


def check() -> None:
    """Raise ConfigurationError listing every invalid setting, if there is one."""
    problems = ERRORS + _semantic_errors()
    if problems:
        raise ConfigurationError("invalid configuration:\n  " + "\n  ".join(problems))


def describe() -> list[dict[str, Any]]:
    """Every setting with its effective value (secrets masked) and where it came from."""
    rows = []
    for setting in SETTINGS:
        value = globals()[setting.name]
        if setting.kind == "secret" and value:
            value = "****"
        elif isinstance(value, Path):
            value = str(value)
        rows.append({
            "key": setting.key, "env": setting.env, "value": value,
            "source": SOURCES.get(setting.name, "default"), "help": setting.help,
        })
    return rows


# Filled by _apply; declared for readers and type checkers.
SOURCES: dict[str, str] = {}
ERRORS: list[str] = []
CONFIG_FILE: Path | None = None

LLM_PROVIDER: str
OLLAMA_HOST: str
LLM_BASE_URL: str
LLM_API_KEY: str | None
TEXT_MODEL: str
VISION_MODEL: str
MAX_EXTRACT_RETRIES: int
EXTRACT_CHUNK_CHARS: int
VISION_TIMEOUT_S: float
LLM_HOSTED_TIMEOUT_S: float
LLM_RETRIES: int
ENABLE_THINKING: bool
LLM_CONCURRENCY: int
OCR_BACKEND: str
OCR_FALLBACKS: list[str]
OCR_LANGUAGES: list[str]
OCR_MIN_CONFIDENCE: float
OCR_DETECT_ROTATION: bool
OCR_DESKEW: bool
OCR_DPI: int
TESSERACT_PSM: int
OCR_CONCURRENCY: int
OCR_QUALITY_CHECK: bool
OCR_QUALITY_MIN_CONFIDENCE: float
PADDLE_DEVICE: str
PADDLE_MODEL: str
PADDLE_TABLES: bool
DOCLING_TABLE_MODE: str
DOCLING_CELL_MATCHING: bool
INCLUDE_LAYOUT: bool
LAYOUT_MARKERS: bool
TFIDF_CONFIDENCE_FLOOR: float
MIN_CLASSIFICATION_CONFIDENCE: float
MIN_SOURCE_CONFIDENCE: float
REVIEW_QUEUE_ENABLED: bool
REVIEW_DATABASE_URL: str
REVIEW_LOCK_SECONDS: int
REVIEW_DOCUMENTS_DIR: Path
MAX_FILE_BYTES: int
MAX_IMAGE_PIXELS: int
MAX_PDF_PAGES: int
BATCH_WORKERS: int
MAX_BATCH_FILES: int
MAX_BATCH_BYTES: int
MAX_CONCURRENT_JOBS: int
API_KEY: str | None
API_CORS_ORIGINS: list[str]
JOBS_DIR: Path
EINVOICE_RESOURCES: Path | None
EINVOICE_DOWNLOADS: Path | None
CLOUD_EQUIVALENT_USD_PER_1M_TOKENS: float
LANGFUSE_PUBLIC_KEY: str | None
LANGFUSE_SECRET_KEY: str | None
LANGFUSE_HOST: str

configure()
