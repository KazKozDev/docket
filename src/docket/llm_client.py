"""The pipeline's one door to a language model.

Two calls go through it: `chat_json` (classification, extraction, the OCR
quality check) and `vision_transcribe` (the vlm OCR backend). Both hand the
work to an `LLMBackend`: the one passed as `ProcessOptions(llm=...)`, else
the built-in one `config.LLM_PROVIDER` names — `OllamaBackend` or
`OpenAICompatibleBackend`, which also covers vLLM, llama.cpp's llama-server,
LM Studio and hosted APIs. Nothing upstream depends on which one is in use.

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
import logging
import random
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable

import httpx

from . import config, limits

log = logging.getLogger("docket")


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    """A model's answer and the token counts its provider reported, if any."""

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LLMBackend(Protocol):
    """What the pipeline needs from a language model.

    Implement it to route docket through your own client (an SDK, a gateway,
    a model loaded in-process) and pass it as `ProcessOptions(llm=...)`.
    `text_model` and `vision_model` name the models for usage metrics and
    traces; an empty `vision_model` means the vlm OCR backend is unavailable.
    Raise `LLMError` on failure — any other exception is wrapped into one.

    `schema` is the JSON Schema the answer must match, when there is one:
    pass it to constrained decoding if your runtime has it. The answer is
    validated against it either way, so best-effort is fine.
    """

    @property
    def text_model(self) -> str: ...

    @property
    def vision_model(self) -> str: ...

    def generate_json(self, prompt: str, *, schema: dict | None, timeout: float) -> LLMReply:
        """Answer `prompt` with one JSON object, as text."""
        ...

    def transcribe_image(self, image: bytes, *, mime: str, prompt: str, timeout: float) -> LLMReply:
        """Answer `prompt` about the image, as plain text."""
        ...


@dataclass
class _Usage:
    calls: int = 0
    estimated_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unreported_calls: int = 0
    models: list[str] = field(default_factory=list)

    def record(self, *texts: str, model: str | None = None, reply: LLMReply | None = None) -> None:
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

    def record(self, *texts: str, model: str | None = None, reply: LLMReply | None = None) -> None:
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
        backend = OpenAICompatibleBackend()
        try:
            resp = httpx.get(f"{backend.base_url}/models", headers=backend._headers(), timeout=timeout)
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


_VISION_PROMPT = (
    "Transcribe every piece of text visible in this document image, "
    "verbatim, preserving line order. Where the layout shows columns, "
    "render them as markdown table rows so cell boundaries survive. "
    "Copy every digit exactly as printed; never recalculate or tidy up "
    "numbers. Output plain text only, no commentary."
)


_backend_var: ContextVar[LLMBackend | None] = ContextVar("docket_llm_backend", default=None)


def default_backend() -> LLMBackend:
    """The built-in backend `config.LLM_PROVIDER` names. It reads its host,
    models and key from `config` at call time, so a UI that writes a picked
    model to `config.TEXT_MODEL` changes the next call."""
    return OpenAICompatibleBackend() if config.LLM_PROVIDER == "openai" else OllamaBackend()


def current_backend() -> LLMBackend:
    """The backend of the document being processed, else the default one."""
    return _backend_var.get() or default_backend()


@contextmanager
def use_backend(backend: LLMBackend | None) -> Iterator[None]:
    """Route this context's LLM calls to `backend`; None keeps the current one.
    Context-local, so concurrent documents can use different backends."""
    if backend is None:
        yield
        return
    token = _backend_var.set(backend)
    try:
        yield
    finally:
        _backend_var.reset(token)


def _call(fn, *args, **kwargs) -> LLMReply:
    with limits.slot("llm"):
        try:
            return fn(*args, **kwargs)
        except LLMError:
            raise
        except Exception as exc:  # a custom backend's own errors are model failures too
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc


def chat_json(prompt: str, *, timeout: float = 120.0, schema: dict | None = None) -> dict:
    """Send a prompt, ask for a JSON object back, return it parsed.

    `schema` goes to the backend for constrained decoding where the runtime
    has it; callers still include it in the prompt and validate the answer.
    """
    backend = current_backend()
    model = backend.text_model
    start = time.monotonic()
    reply = _call(backend.generate_json, prompt, schema=schema, timeout=timeout)
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


def vision_transcribe(image: bytes, *, mime: str = "image/png", timeout: float | None = None) -> str:
    """Ask a vision model to transcribe all visible text in an image, verbatim.

    Takes encoded image bytes, not a path: callers render pages in memory, so
    no temporary file is ever written next to the user's document.
    """
    backend = current_backend()
    model = backend.vision_model
    if not model:
        raise LLMError("no vision model is configured")
    timeout = config.VISION_TIMEOUT_S if timeout is None else timeout
    start = time.monotonic()
    reply = _call(backend.transcribe_image, image, mime=mime, prompt=_VISION_PROMPT, timeout=timeout)
    text = reply.content.strip()
    usage.record(_VISION_PROMPT, text, model=model, reply=reply)
    _trace("vision_transcribe", model, "<image>", text, start)
    return text


class OllamaBackend:
    """Ollama's native /api/chat, local or cloud. Every argument left None is
    read from `config` (OLLAMA_HOST, DOCKET_TEXT_MODEL, DOCKET_VISION_MODEL,
    DOCKET_ENABLE_THINKING) at call time."""

    def __init__(
        self,
        *,
        host: str | None = None,
        text_model: str | None = None,
        vision_model: str | None = None,
        think: bool | None = None,
    ) -> None:
        self._host, self._text_model, self._vision_model, self._think = host, text_model, vision_model, think

    @property
    def host(self) -> str:
        return (self._host or config.OLLAMA_HOST).rstrip("/")

    @property
    def text_model(self) -> str:
        return config.TEXT_MODEL if self._text_model is None else self._text_model

    @property
    def vision_model(self) -> str:
        return config.VISION_MODEL if self._vision_model is None else self._vision_model

    @property
    def think(self) -> bool:
        return config.ENABLE_THINKING if self._think is None else self._think

    def generate_json(self, prompt: str, *, schema: dict | None, timeout: float) -> LLMReply:
        model = self.text_model
        return self._chat({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Ollama cloud does not enforce JSON Schema decoding. Keep its
            # supported JSON mode; extraction still includes and validates schema.
            "format": schema if schema and not _cloud_model(model) else "json",
            "stream": False,
            "think": self.think,
            "options": {"temperature": 0},
        }, timeout=timeout)

    def transcribe_image(self, image: bytes, *, mime: str, prompt: str, timeout: float) -> LLMReply:
        return self._chat({
            "model": self.vision_model,
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}],
            "stream": False,
            "think": self.think,
            "options": {"temperature": 0},
        }, timeout=timeout)

    def _chat(self, payload: dict, *, timeout: float) -> LLMReply:
        model = payload["model"]
        try:
            resp = _post(f"{self.host}/api/chat", hosted=_cloud_model(model), json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        body = resp.json()
        return LLMReply(body["message"]["content"], body.get("prompt_eval_count"), body.get("eval_count"))


class _SchemaRejected(Exception):
    """The server answered 400/422 to a json_schema response format."""


# (base_url, model) pairs whose server refused json_schema but took
# json_object: asked once per process, not on every call.
_JSON_SCHEMA_REFUSED: set[tuple[str, str]] = set()


class OpenAICompatibleBackend:
    """Any OpenAI-compatible Chat Completions API: vLLM, llama.cpp's
    llama-server, LM Studio, LocalAI, Ollama's /v1, or a hosted API.

    With a schema it asks for `response_format: json_schema` (non-strict:
    Pydantic schemas rarely meet strict mode's rules), which vLLM, llama.cpp
    and LM Studio turn into constrained decoding; a server that refuses it
    gets `json_object` instead, from then on. `structured_output="json_object"`
    skips the attempt. Arguments left None come from `config`
    (DOCKET_LLM_BASE_URL, DOCKET_LLM_API_KEY, DOCKET_TEXT_MODEL,
    DOCKET_VISION_MODEL, DOCKET_LLM_STRUCTURED_OUTPUT) at call time.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        text_model: str | None = None,
        vision_model: str | None = None,
        structured_output: str | None = None,
    ) -> None:
        if structured_output not in (None, "json_schema", "json_object"):
            raise ValueError("structured_output must be 'json_schema' or 'json_object'")
        self._base_url, self._api_key = base_url, api_key
        self._text_model, self._vision_model = text_model, vision_model
        self._structured_output = structured_output

    @property
    def base_url(self) -> str:
        return (self._base_url or config.LLM_BASE_URL).rstrip("/")

    @property
    def api_key(self) -> str | None:
        return self._api_key or config.LLM_API_KEY

    @property
    def text_model(self) -> str:
        return config.TEXT_MODEL if self._text_model is None else self._text_model

    @property
    def vision_model(self) -> str:
        return config.VISION_MODEL if self._vision_model is None else self._vision_model

    @property
    def structured_output(self) -> str:
        return self._structured_output or config.LLM_STRUCTURED_OUTPUT

    @property
    def hosted(self) -> bool:
        return _hosted_url(self.base_url)

    def generate_json(self, prompt: str, *, schema: dict | None, timeout: float) -> LLMReply:
        model = self.text_model
        messages = [{"role": "user", "content": prompt}]
        key = (self.base_url, model)
        if schema and self.structured_output == "json_schema" and key not in _JSON_SCHEMA_REFUSED:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "docket_output", "schema": schema, "strict": False},
            }
            try:
                return self._chat(model, messages, timeout=timeout, response_format=response_format)
            except _SchemaRejected:
                reply = self._chat(model, messages, timeout=timeout, response_format={"type": "json_object"})
                _JSON_SCHEMA_REFUSED.add(key)
                log.warning("server refused json_schema; using json_object",
                            extra={"base_url": self.base_url, "model": model})
                return reply
        return self._chat(model, messages, timeout=timeout, response_format={"type": "json_object"})

    def transcribe_image(self, image: bytes, *, mime: str, prompt: str, timeout: float) -> LLMReply:
        b64 = base64.b64encode(image).decode()
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }
        return self._chat(self.vision_model, [message], timeout=timeout)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _chat(
        self, model: str, messages: list[dict], *, timeout: float, response_format: dict | None = None
    ) -> LLMReply:
        payload: dict = {"model": model, "messages": messages, "temperature": 0}
        if response_format is not None:
            payload["response_format"] = response_format
        try:
            resp = _post(
                f"{self.base_url}/chat/completions",
                hosted=self.hosted,
                json=payload,
                headers=self._headers(),
                timeout=timeout,
            )
            body = resp.json()
            reported = body.get("usage") or {}
            return LLMReply(
                body["choices"][0]["message"]["content"] or "",
                reported.get("prompt_tokens"),
                reported.get("completion_tokens"),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 422) and (response_format or {}).get("type") == "json_schema":
                raise _SchemaRejected from exc
            raise LLMError(f"LLM request failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {exc}") from exc


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


def _cloud_model(model: str) -> bool:
    """Ollama cloud models: `name:cloud`, `name:tag-cloud`."""
    return model.endswith((":cloud", "-cloud"))


def _hosted_url(base_url: str) -> bool:
    """An OpenAI-compatible endpoint that is not on this machine."""
    return httpx.URL(base_url).host not in ("localhost", "127.0.0.1", "::1")


def _post(url: str, *, hosted: bool, json: dict, timeout: float, headers: dict | None = None) -> httpx.Response:
    """POST with the stall handling above: hosted models get a short timeout
    and retries on timeouts too; every model is retried on 429/5xx and
    connection errors."""
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
