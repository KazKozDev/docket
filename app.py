"""Streamlit demo: upload a document, watch it go through
OCR/VLM -> classify -> extract -> validate, see the result at each stage.

    streamlit run app.py
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import streamlit as st

from docket import config, llm_client
from docket.pipeline import process
from docket.schemas import PipelineResult

GOLDEN_DIR = Path(__file__).parent / "eval" / "golden_dataset"

st.set_page_config(page_title="docket", layout="wide")

st.title("docket")
st.caption("Extract, classify and validate structured data from business documents — runs on Ollama.")

# The model pickers below write straight back to `config`. Every call site
# reads `config.TEXT_MODEL` / `config.VISION_MODEL` at call time, so a
# selection takes effect on the next run without threading a model argument
# through the whole pipeline. That's process-global state, which is fine for
# a local single-user demo and would not be for a shared deployment.

with st.sidebar:
    st.header("Input")
    mode = st.radio("Source", ["Try a sample", "Upload a file"], label_visibility="collapsed")

    uploaded_path: Path | None = None

    if mode == "Try a sample":
        samples = sorted(
            p for ext in ("*.txt", "*.pdf", "*.png", "*.jpg") for p in GOLDEN_DIR.glob(ext)
        )
        if samples:
            choice = st.selectbox("Sample document", samples, format_func=lambda p: p.name)
            uploaded_path = choice
        else:
            st.info("No sample documents found in eval/golden_dataset/.")
    else:
        uploaded = st.file_uploader("Document", type=["pdf", "png", "jpg", "jpeg", "txt"])
        if uploaded is not None:
            suffix = Path(uploaded.name).suffix
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(uploaded.read())
            tmp.close()
            uploaded_path = Path(tmp.name)

    st.divider()
    st.subheader("Models")

    text_models = llm_client.list_models()
    vision_models = llm_client.list_models(vision_only=True)

    if not text_models:
        st.warning(
            f"No models found — is Ollama running at {config.OLLAMA_HOST}? "
            f"Falling back to the configured defaults."
        )
        st.code(f"text:   {config.TEXT_MODEL}\nvision: {config.VISION_MODEL}", language=None)
    else:
        def _index(options: list[str], current: str) -> int:
            return options.index(current) if current in options else 0

        config.TEXT_MODEL = st.selectbox(
            "Text model", text_models, index=_index(text_models, config.TEXT_MODEL)
        )
        config.VISION_MODEL = st.selectbox(
            "Vision model", vision_models, index=_index(vision_models, config.VISION_MODEL)
        )
        st.caption(
            f"{len(text_models)} models available, {len(vision_models)} of them vision-capable. "
            f"Defaults come from .env; a choice here applies to the next run."
        )

    thinking = st.checkbox(
        "Model thinking",
        value=config.ENABLE_THINKING,
        help=(
            "Reasoning models deliberate before answering. Transcribing fields off a page "
            "gives them nothing to deliberate about — leaving this off was ~18x faster "
            "end-to-end for identical output."
        ),
    )
    config.ENABLE_THINKING = thinking

if uploaded_path is None:
    st.info("Pick a sample or upload a document to run the pipeline.")
    st.stop()

col_preview, col_result = st.columns([1, 1.4], gap="large")

with col_preview:
    st.subheader("Document")
    suffix = uploaded_path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        st.image(str(uploaded_path), use_container_width=True)
    elif suffix == ".pdf":
        st.caption("PDF preview not rendered here — see extracted text in the pipeline output. →")
    else:
        st.text(uploaded_path.read_text()[:3000])

with col_result:
    st.subheader("Pipeline")

    # One spinner for the whole run makes a 60-second document look frozen.
    # The pipeline already emits per-stage events for the TUI; reusing them
    # here turns dead air into visible progress.
    progress = st.empty()
    stage_log: list[str] = []

    def on_stage(stage: str, payload: object) -> None:
        elapsed_so_far = time.time() - start
        if stage == "ocr":
            stage_log.append(f"Text acquired via **{payload.method}** ({len(payload.text)} chars) — {elapsed_so_far:.1f}s")
        elif stage == "classify":
            stage_log.append(f"Classified as **{payload.type_name}** via {payload.method} — {elapsed_so_far:.1f}s")
        elif stage == "extract":
            label = "schema validated" if payload is not None else "failed"
            stage_log.append(f"Extraction {label} — {elapsed_so_far:.1f}s")
        elif stage == "validate":
            stage_log.append(f"Validation done — {elapsed_so_far:.1f}s")
        progress.markdown("\n\n".join(stage_log))

    start = time.time()
    with st.spinner("Running OCR/VLM → classify → extract → validate..."):
        try:
            result: PipelineResult = process(uploaded_path, on_stage=on_stage)
        except Exception as exc:  # noqa: BLE001 — surface any pipeline failure to the demo UI
            st.error(f"Pipeline failed: {exc}")
            st.stop()
        elapsed = time.time() - start
    progress.empty()

    stage_cols = st.columns(4)
    stage_cols[0].metric("OCR method", result.ocr_method)
    stage_cols[1].metric("Doc type", result.classification.type_name)
    stage_cols[2].metric("Classified via", result.classification.method)
    stage_cols[3].metric("Extract attempts", result.extract_attempts)

    st.caption(
        f"Classification confidence: {result.classification.confidence:.0%} · "
        f"{result.raw_text_chars} chars of text acquired · {result.llm_calls} LLM call(s) · "
        f"{elapsed:.1f}s total"
    )

    if result.needs_review:
        st.warning(
            "**Queued for human review** — "
            + "; ".join(result.review_reasons)
        )

    if result.extracted is None:
        st.error("Extraction failed — no structured output could be validated against the schema.")
    else:
        st.markdown("**Extracted fields**")
        st.json(result.extracted, expanded=True)

    st.markdown("**Validation**")
    if not result.validation_issues:
        st.success("No issues — all business rules passed.")
    else:
        for issue in result.validation_issues:
            if issue.severity == "error":
                st.error(f"**{issue.field}** — {issue.message}")
            else:
                st.warning(f"**{issue.field}** — {issue.message}")

    with st.expander("Raw classification scores"):
        st.json(result.classification.scores)

    with st.expander("Full pipeline result (JSON)"):
        st.code(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False), language="json")
