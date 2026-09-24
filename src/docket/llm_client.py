"""Thin wrapper around the LLM backend: Ollama or any OpenAI-compatible API.

Kept deliberately small: one function for a plain text/JSON chat call, one
for a vision call. `config.LLM_PROVIDER` picks the transport; nothing
upstream depends on which one is in use.

Two pieces of production plumbing live here rather than upstream, because
every LLM call in the pipeline goes through this module:

- `usage`: a per-document counter of calls, the models used, the tokens
  the provider reported (Ollama's prompt_eval_count/eval_count, OpenAI's
  usage block) and a chars/4 estimate for providers that report nothing.
- Langfuse tracing: if LANGFUSE_PUBLIC_KEY/SECRET_KEY are set, every call is
  wrapped in a Langfuse generation span. If they aren't, or the langfuse
  package isn't installed, tracing is a no-op — this stays local-first by
  default and only reaches out when explicitly configured.
"""
from __future__ import annotations

import base64
import json
import random
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from . import config, limits


class LLMError(RuntimeError):
    pass


@dataclass
class _Reply:
    """A model's answer and the token counts its provider reported, if any."""

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class _Usage:
    calls: int = 0
    estimated_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unreported_calls: int = 0
    models: list[str] = field(default_factory=list)

    def record(self, *texts: str, model: str | None = None, reply: _Reply | None = None) -> None:
        self.calls += 1
        # Rough token estimate (chars/4), kept for providers that report
        # nothing; not meant to match a real tokenizer.
        self.estimated_tokens += sum(len(t) for t in texts) // 4
        if reply is not None and reply.input_tokens is not None and reply.output_tokens is not None:
            self.input_tokens += reply.input_tokens
            self.output_tokens += reply.output_tokens
        else:
            self.unreported_calls += 1
        if model and model not in self.models:
            self.models.append(model)

    def snapshot(self) -> dict:
        return {
            "calls": self.calls, "estimated_tokens": self.estimated_tokens,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "unreported_calls": self.unreported_calls, "models": list(self.models),
        }


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

    @property
    def input_tokens(self) -> int:
        return self._current().input_tokens

    @property
    def output_tokens(self) -> int:
        return self._current().output_tokens

    @property
    def unreported_calls(self) -> int:
        return self._current().unreported_calls

    @property
    def models(self) -> list[str]:
        return list(self._current().models)

    def record(self, *texts: str, model: str | None = None, reply: _Reply | None = None) -> None:
        self._current().record(*texts, model=model, reply=reply)

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
    except Exception:  # noqa: BLE001 — a broken tracing setup must not break extraction
        return None


def _trace(name: str, model: str, input_: object, output: object, start: float) -> None:
    client = _get_langfuse()
    if client is None:
        return
    # Prompts carry the document's text — invoices, contracts, bank details.
    # They leave the machine only when the operator says so.
    content = {"input": input_, "output": output} if config.LANGFUSE_CONTENT else {}
    try:
        client.start_observation(
            name=name,
            as_type="generation",
            model=model,
            **content,
            metadata={
                "latency_s": round(time.monotonic() - start, 3),
                "input_chars": len(str(input_)),
                "output_chars": len(str(output)),
            },
        ).end()
        client.flush()
    except Exception:  # noqa: BLE001, S110 — tracing must never break the pipeline
        pass


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
        reply = _openai_chat(
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
        reply = _ollama_chat(payload, timeout=timeout)

    content = reply.content
    usage.record(prompt, content, model=model, reply=reply)
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
        reply = _openai_chat(model, [message], timeout=timeout)
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt_text, "images": [b64]}],
            "stream": False,
            "think": config.ENABLE_THINKING,
            "options": {"temperature": 0},
        }
        reply = _ollama_chat(payload, timeout=timeout)

    text = reply.content.strip()
    usage.record(prompt_text, text, model=model, reply=reply)
    _trace("vision_transcribe", model, "<image>", text, start)
    return text


# A hosted model answers in seconds (p99 ~40 s on the eval corpus), so one
# that has said nothing for a minute has stalled: waiting out a local-model
# timeout of several minutes, as Ollama cloud's 5-minute 502s showed, just
# stalls the document. Local models keep their long timeouts and are never
# retried after one: a laptop can honestly need minutes for a dense page.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_S = 2.0
# A Retry-After longer than this is not waited out: the document fails with
# the provider's error instead of holding a worker for minutes.
_MAX_RETRY_AFTER_S = 60.0


def _retry_delay(attempt: int, resp: httpx.Response | None = None) -> float:
    """How long to wait before retry `attempt` + 1.

    The provider's Retry-After (seconds or an HTTP date) wins when it sends
    one, capped at _MAX_RETRY_AFTER_S. Otherwise exponential backoff with
    jitter, so parallel workers throttled together don't retry together.
    """
    header = resp.headers.get("retry-after") if resp is not None else None
    if header:
        try:
            seconds = float(header)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(header).timestamp() - time.time()
            except (TypeError, ValueError):
                seconds = None
        if seconds is not None:
            return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)
    return _BACKOFF_S * (2 ** attempt) * random.uniform(0.5, 1.5)


def _hosted(model: str) -> bool:
    """Ollama cloud models (`name:cloud`, `name:tag-cloud`) and any
    OpenAI-compatible endpoint that is not on this machine."""
    if config.LLM_PROVIDER == "openai":
        host = httpx.URL(config.LLM_BASE_URL).host
        return host not in ("localhost", "127.0.0.1", "::1")
    return model.endswith((":cloud", "-cloud"))


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
                time.sleep(_retry_delay(attempt, resp))
                continue
            resp.raise_for_status()
            return resp
        except httpx.TimeoutException:
            if not hosted or last:
                raise
        except httpx.TransportError:
            if last:
                raise
        time.sleep(_retry_delay(attempt))
    raise AssertionError("unreachable")


def _ollama_chat(payload: dict, *, timeout: float) -> _Reply:
    with limits.slot("llm"):
        return _ollama_request(payload, timeout=timeout)


def _ollama_request(payload: dict, *, timeout: float) -> _Reply:
    try:
        resp = _post(f"{config.OLLAMA_HOST}/api/chat", model=payload["model"], json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LLMError(f"Ollama request failed: {exc}") from exc
    body = resp.json()
    return _Reply(body["message"]["content"], body.get("prompt_eval_count"), body.get("eval_count"))


def _openai_headers() -> dict:
    return {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}


def _openai_chat(
    model: str, messages: list[dict], *, timeout: float, json_mode: bool = False
) -> _Reply:
    with limits.slot("llm"):
        return _openai_request(model, messages, timeout=timeout, json_mode=json_mode)


def _openai_request(
    model: str, messages: list[dict], *, timeout: float, json_mode: bool = False
) -> _Reply:
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
        body = resp.json()
        reported = body.get("usage") or {}
        return _Reply(
            body["choices"][0]["message"]["content"] or "",
            reported.get("prompt_tokens"),
            reported.get("completion_tokens"),
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"Unexpected LLM response shape: {exc}") from exc
