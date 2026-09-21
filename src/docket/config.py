import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# LLM backend. "ollama" (default) talks to a local or cloud Ollama server;
# "openai" talks to any OpenAI-compatible Chat Completions endpoint — Mistral
# La Plateforme (EU-hosted), OpenAI, Azure OpenAI, vLLM, LM Studio, etc.
LLM_PROVIDER = os.getenv("DOCKET_LLM_PROVIDER", "ollama").lower()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLM_BASE_URL = os.getenv("DOCKET_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("DOCKET_LLM_API_KEY")
TEXT_MODEL = os.getenv("DOCKET_TEXT_MODEL", "deepseek-v4.1-flash:cloud")
VISION_MODEL = os.getenv("DOCKET_VISION_MODEL", "deepseek-v4.1-flash:cloud")
MAX_EXTRACT_RETRIES = int(os.getenv("DOCKET_MAX_EXTRACT_RETRIES", "2"))
EXTRACT_CHUNK_CHARS = int(os.getenv("DOCKET_EXTRACT_CHUNK_CHARS", "12000"))

# A full-page scan through a local vision model is slow — 40s+ is normal on a
# laptop, and a dense form can take several minutes. Too low a timeout turns a
# slow document into a failed one.
VISION_TIMEOUT_S = float(os.getenv("DOCKET_VISION_TIMEOUT_S", "300"))

# Reasoning models burn output tokens on internal deliberation before
# answering. This pipeline hands the model a JSON Schema and asks it to
# transcribe fields off a page — there is nothing to deliberate about, and
# the reasoning tokens are pure latency. Measured on one invoice with
# deepseek-v4.1-flash:cloud: 11.9s / 661 output tokens with thinking on,
# 5.1s / 171 tokens with it off, for a byte-identical answer.
# Ollama ignores this for models that don't think, so it's safe to always send.
ENABLE_THINKING = os.getenv("DOCKET_ENABLE_THINKING", "false").lower() in {
    "1",
    "true",
    "yes",
}

# Ask a cheap text model whether OCR text is garbled, for documents where no
# deterministic signal can tell (a contract has no arithmetic to fail). Costs
# ~1.3s and only runs on OCR-derived text. See ocr_quality.py for why the two
# free alternatives were measured and rejected.
OCR_QUALITY_CHECK = os.getenv("DOCKET_OCR_QUALITY_CHECK", "true").lower() in {
    "1",
    "true",
    "yes",
}
OCR_QUALITY_MIN_CONFIDENCE = float(
    os.getenv("DOCKET_OCR_QUALITY_MIN_CONFIDENCE", "0.7")
)

# Below this many extracted characters per PDF page, we treat the page as a
# scan (no text layer) and fall back to OCR / VLM instead of pdfplumber text.
MIN_CHARS_PER_PAGE = 20

# OCR backend chain. DOCKET_OCR_BACKEND names the primary engine ("auto"
# picks the first installed of tesseract, paddle); DOCKET_OCR_FALLBACKS is a
# comma-separated list tried in order when a page's reading is rejected.
# An explicitly named backend that cannot run is a startup error.
OCR_BACKEND = os.getenv("DOCKET_OCR_BACKEND", "auto").strip().lower()
OCR_FALLBACKS = [
    b.strip().lower()
    for b in os.getenv("DOCKET_OCR_FALLBACKS", "vlm").split(",")
    if b.strip()
]
# ISO 639-1 codes, comma-separated (e.g. "en,de,fr"). Each backend maps them
# to its own names; each extra language slows Tesseract, so list only the
# ones your documents use.
OCR_LANGUAGES = os.getenv("DOCKET_OCR_LANGUAGES", "en")
# A page reading is accepted when its confidence (0..1) reaches this.
OCR_MIN_CONFIDENCE = float(os.getenv("DOCKET_OCR_MIN_CONFIDENCE", "0.60"))
OCR_DETECT_ROTATION = os.getenv("DOCKET_OCR_DETECT_ROTATION", "true").lower() in {
    "1",
    "true",
    "yes",
}
# Compute device for PaddleOCR ("cpu", "gpu", "gpu:0").
PADDLE_DEVICE = os.getenv("DOCKET_PADDLE_DEVICE", "cpu")
# Tesseract page-segmentation mode and PDF render resolution. PSM 3 = fully
# automatic; a dense table scan may read better at PSM 6 / higher DPI.
TESSERACT_PSM = os.getenv("DOCKET_TESSERACT_PSM", "3")
OCR_DPI = int(os.getenv("DOCKET_OCR_DPI", "200"))
MAX_FILE_BYTES = int(os.getenv("DOCKET_MAX_FILE_BYTES", str(20 * 1024 * 1024)))
MAX_PDF_PAGES = int(os.getenv("DOCKET_MAX_PDF_PAGES", "100"))
MAX_CONCURRENT_JOBS = int(os.getenv("DOCKET_MAX_CONCURRENT_JOBS", "2"))
API_KEY = os.getenv("DOCKET_API_KEY")

# Confidence floor the TF-IDF classifier must clear to be trusted over an
# LLM call — below this, classify() still escalates even though the rules
# were ambiguous and the TF-IDF model did produce an answer.
TFIDF_CONFIDENCE_FLOOR = float(os.getenv("DOCKET_TFIDF_CONFIDENCE_FLOOR", "0.65"))

# Below this classification confidence, or on any error-severity validation
# issue / failed extraction, a document is queued for human review instead
# of being treated as a clean result. See review_queue.py.
MIN_CLASSIFICATION_CONFIDENCE = float(os.getenv("DOCKET_MIN_CONFIDENCE", "0.55"))
REVIEW_QUEUE_ENABLED = os.getenv("DOCKET_REVIEW_QUEUE_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
}
REVIEW_QUEUE_PATH = Path(os.getenv("DOCKET_REVIEW_QUEUE", "data/review_queue.jsonl"))
REVIEW_DOCUMENTS_DIR = Path(
    os.getenv("DOCKET_REVIEW_DOCUMENTS", "data/review_documents")
)
JOB_STORE_PATH = Path(os.getenv("DOCKET_JOB_STORE", "data/jobs.json"))
JOB_UPLOADS_DIR = Path(os.getenv("DOCKET_JOB_UPLOADS", "data/job_uploads"))

# Illustrative only: what the LLM calls in this run would have cost against a
# small hosted model, at a blended $/1M-token rate. The actual cost of a local
# Ollama run is $0 — this exists purely so eval reports can show the tradeoff
# a team would face before deciding to run this pipeline against a paid API.
CLOUD_EQUIVALENT_USD_PER_1M_TOKENS = float(
    os.getenv("DOCKET_CLOUD_COST_PER_1M_TOKENS", "0.20")
)

# Optional Langfuse tracing — unset by default, so llm_client stays a no-op
# wrapper unless a deployment explicitly opts in.
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
