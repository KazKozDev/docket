"""Thin wrapper around the LLM backend: Ollama or any OpenAI-compatible API.

Kept deliberately small: one function for a plain text/JSON chat call, one
for a vision call. `config.LLM_PROVIDER` picks the transport; nothing
upstream depends on which one is in use.

Two pieces of production plumbing live here rather than upstream, because
every LLM call in the pipeline goes through this module:

- `usage`: a per-run counter of call count and estimated tokens, read by
  the eval harness to report latency/cost per document (see eval/run_eval.py).
- Langfuse tracing: if LANGFUSE_PUBLIC_KEY/SECRET_KEY are set, every call is
  wrapped in a Langfuse generation span. If they aren't, or the langfuse
  package isn't installed, tracing is a no-op — this stays local-first by
  default and only reaches out when explicitly configured.
"""
from __future__ import annotations

import base64
import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

from . import config, limits


class LLMError(RuntimeError):
    pass


@dataclass
class _Usage:
    calls: int = 0
    estimated_tokens: int = 0

    def record(self, *texts: str) -> None:
        self.calls += 1
        # Rough token estimate (chars/4) — good enough for a relative
        # cost/latency comparison, not meant to match a real tokenizer.
        self.estimated_tokens += sum(len(t) for t in texts) // 4

    def reset(self) -> None:
        self.calls = 0
        self.estimated_tokens = 0

    def snapshot(self) -> dict:
        return {"calls": self.calls, "estimated_tokens": self.estimated_tokens}


_usage_var: ContextVar[_Usage | None] = ContextVar("docket_llm_usage", default=None)


class _ContextUsage:
    """Request-local usage facade, safe across concurrent threads/tasks."""

    def _current(self) -> _Usage:
        current = _usage_var.get()
        if current is None:
            current = _Usage()
            _usage_var.set(current)
        return current

    @property
    def calls(self) -> int:
        return self._current().calls

    @property
    def estimated_tokens(self) -> int:
        return self._current().estimated_tokens

    def record(self, *texts: str) -> None:
        self._current().record(*texts)

    def reset(self) -> None:
        _usage_var.set(_Usage())

    def snapshot(self) -> dict:
        return self._current().snapshot()


usage = _ContextUsage()


def _get_langfuse():
    """Returns a Langfuse client if tracing is configured, else None. Import
    and construction are both best-effort: a broken Langfuse setup should
    never take down the extraction pipeline.
    """
    if not (config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            host=config.LANGFUSE_HOST,
        )
    except Exception:
        return None


def _trace(name: str, model: str, input_: object, output: object, start: float) -> None:
    client = _get_langfuse()
    if client is None:
        return
    try:
        client.start_observation(
            name=name,
            as_type="generation",
            model=model,
            input=input_,
            output=output,
            metadata={"latency_s": round(time.monotonic() - start, 3)},
        ).end()
        client.flush()
    except Exception:
        pass  # tracing must never break the pipeline


def list_models(*, vision_only: bool = False, timeout: float = 10.0) -> list[str]:
    """Model names this Ollama server can serve for the pipeline's work, sorted.

    Filtered on the capabilities `/api/tags` reports, not on name-guessing:
    every model offered must do `completion` (which excludes embedding-only
    models like bge-m3 that would fail the moment they were picked), and the
    vision list additionally requires `vision` — obvious for
    "granite3.2-vision", not at all obvious for "ministral-3".

    Returns an empty list if Ollama isn't reachable; callers should treat
    that as "offer no choices", not as an error worth crashing a UI over.
    """
    if config.LLM_PROVIDER == "openai":
        # /models reports no capabilities, so every model is offered as-is.
        try:
            resp = httpx.get(
                f"{config.LLM_BASE_URL}/models", headers=_openai_headers(), timeout=timeout
            )
            resp.raise_for_status()
            return sorted(m["id"] for m in resp.json().get("data", []))
        except (httpx.HTTPError, ValueError, KeyError):
            return []
    try:
        resp = httpx.get(f"{config.OLLAMA_HOST}/api/tags", timeout=timeout)
        resp.raise_for_status()
        models = resp.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return []

    required = {"completion", "vision"} if vision_only else {"completion"}
    names = [m["name"] for m in models if required <= set(m.get("capabilities") or [])]
    return sorted(names)


def chat_json(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = 120.0,
    schema: dict | None = None,
) -> dict:
    """Send a prompt, ask for a JSON object back, return it parsed.

    Local models receive `schema` for constrained decoding. Cloud models
    use JSON mode; callers must include the schema in the prompt and validate.
    """
    model = model or config.TEXT_MODEL
    start = time.monotonic()
    if config.LLM_PROVIDER == "openai":
        content = _openai_chat(
            model,
            [{"role": "user", "content": prompt}],
            timeout=timeout,
            json_mode=True,
        )
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Ollama cloud does not enforce JSON Schema decoding. Keep its
            # supported JSON mode; extraction still includes and validates schema.
            "format": schema if schema and not model.endswith(":cloud") else "json",
            "stream": False,
            "think": config.ENABLE_THINKING,
            "options": {"temperature": 0},
        }
        content = _ollama_chat(payload, timeout=timeout)

    usage.record(prompt, content)
    _trace("chat_json", model, prompt, content, start)
    try:
        # Some cloud responses wrap otherwise valid JSON in a Markdown fence.
        # Strip only an enclosing fence, never guess JSON out of arbitrary prose.
        fenced = re.fullmatch(
            r"\s*```(?:json)?\s*\n?(.*?)\n?```\s*", content, re.DOTALL
        )
        parsed = json.loads(fenced.group(1) if fenced else content)
        if not isinstance(parsed, dict):
            raise LLMError("Model returned JSON that is not an object")
        return parsed
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model did not return valid JSON: {content[:200]!r}") from exc


def vision_transcribe(
    image: bytes,
    *,
    mime: str = "image/png",
    model: str | None = None,
    timeout: float | None = None,
) -> str:
    """Ask a vision model to transcribe all visible text in an image, verbatim.

    Takes encoded image bytes, not a path: callers render pages in memory, so
    no temporary file is ever written next to the user's document.
    """
    model = model or config.VISION_MODEL
    timeout = config.VISION_TIMEOUT_S if timeout is None else timeout
    b64 = base64.b64encode(image).decode()

    prompt_text = (
        "Transcribe every piece of text visible in this document image, "
        "verbatim, preserving line order. Where the layout shows columns, "
        "render them as markdown table rows so cell boundaries survive. "
        "Copy every digit exactly as printed; never recalculate or tidy up "
        "numbers. Output plain text only, no commentary."
    )
    start = time.monotonic()
    if config.LLM_PROVIDER == "openai":
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }
        text = _openai_chat(model, [message], timeout=timeout).strip()
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt_text, "images": [b64]}],
            "stream": False,
            "think": config.ENABLE_THINKING,
            "options": {"temperature": 0},
        }
        text = _ollama_chat(payload, timeout=timeout).strip()

    usage.record(prompt_text, text)
    _trace("vision_transcribe", model, "<image>", text, start)
    return text


# A hosted model answers in seconds (p99 ~40 s on the eval corpus), so one
# that has said nothing for a minute has stalled: waiting out a local-model
# timeout of several minutes, as Ollama cloud's 5-minute 502s showed, just
# stalls the document. Local models keep their long timeouts and are never
# retried after one: a laptop can honestly need minutes for a dense page.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_S = 2.0


def _hosted(model: str) -> bool:
    """Ollama cloud models (`name:cloud`, `name:tag-cloud`) and any
    OpenAI-compatible endpoint that is not on this machine."""
    if config.LLM_PROVIDER == "openai":
        host = httpx.URL(config.LLM_BASE_URL).host
        return host not in ("localhost", "127.0.0.1", "::1")
    return model.endswith(":cloud") or model.endswith("-cloud")


def _post(url: str, *, model: str, json: dict, timeout: float, headers: dict | None = None) -> httpx.Response:
    """POST with the stall handling above: hosted models get a short timeout
    and retries on timeouts too; every model is retried on 429/5xx and
    connection errors."""
    hosted = _hosted(model)
    if hosted:
        timeout = min(timeout, config.LLM_HOSTED_TIMEOUT_S)
    attempts = config.LLM_RETRIES + 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            resp = httpx.post(url, json=json, headers=headers, timeout=timeout)
            if resp.status_code in _RETRY_STATUS and not last:
                time.sleep(_BACKOFF_S * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        except httpx.TimeoutException:
            if not hosted or last:
                raise
        except httpx.TransportError:
            if last:
                raise
        time.sleep(_BACKOFF_S * (attempt + 1))
    raise AssertionError("unreachable")


def _ollama_chat(payload: dict, *, timeout: float) -> str:
    with limits.slot("llm"):
        return _ollama_request(payload, timeout=timeout)


def _ollama_request(payload: dict, *, timeout: float) -> str:
    try:
        resp = _post(f"{config.OLLAMA_HOST}/api/chat", model=payload["model"], json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LLMError(f"Ollama request failed: {exc}") from exc
    return resp.json()["message"]["content"]


def _openai_headers() -> dict:
    return {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}


def _openai_chat(
    model: str, messages: list[dict], *, timeout: float, json_mode: bool = False
) -> str:
    with limits.slot("llm"):
        return _openai_request(model, messages, timeout=timeout, json_mode=json_mode)


def _openai_request(
    model: str, messages: list[dict], *, timeout: float, json_mode: bool = False
) -> str:
    payload: dict = {"model": model, "messages": messages, "temperature": 0}
    if json_mode:
        # JSON mode rather than strict json_schema: Pydantic schemas rarely
        # meet strict-mode rules, and extraction validates the result anyway.
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = _post(
            f"{config.LLM_BASE_URL}/chat/completions",
            model=model,
            json=payload,
            headers=_openai_headers(),
            timeout=timeout,
        )
        return resp.json()["choices"][0]["message"]["content"] or ""
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"Unexpected LLM response shape: {exc}") from exc
