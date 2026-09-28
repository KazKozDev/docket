# Language models

Docket needs a text model for classification (when the rules and TF-IDF tiers
aren't confident) and extraction, and a vision model if the `vlm` OCR fallback
is on. It ships no model and picks none: until `DOCKET_TEXT_MODEL` is set,
`process_document` raises `ConfigurationError` (CLI exit code 3).

A model runs wherever you run it. Docket talks to it one of three ways:

| Backend | Talks to | Structured output |
|---|---|---|
| `OllamaBackend` (`DOCKET_LLM_PROVIDER=ollama`, default) | Ollama's `/api/chat`, local or `:cloud` | JSON Schema through `format`; `:cloud` models get JSON mode |
| `OpenAICompatibleBackend` (`DOCKET_LLM_PROVIDER=openai`) | any `/v1/chat/completions`: vLLM, llama.cpp `llama-server`, LM Studio, LocalAI, hosted APIs | `response_format: json_schema`; `json_object` where the server refuses it |
| your own `LLMBackend` | anything | whatever your runtime has |

Extraction always puts the schema in the prompt and validates the answer, so
structured output is not required. It matters most with small local models,
which break JSON far more often without constrained decoding.

## Local recipes

Ollama:

```bash
ollama pull <text-model>
ollama pull <vision-model>        # only for the vlm fallback
export DOCKET_TEXT_MODEL=<text-model> DOCKET_VISION_MODEL=<vision-model>
```

vLLM (one model per server; start a second one for vision):

```bash
vllm serve <hf-model-id> --port 8000
export DOCKET_LLM_PROVIDER=openai DOCKET_LLM_BASE_URL=http://localhost:8000/v1
export DOCKET_TEXT_MODEL=<hf-model-id>
```

llama.cpp:

```bash
llama-server -m model.gguf --port 8080     # add --mmproj <file> for a vision model
export DOCKET_LLM_PROVIDER=openai DOCKET_LLM_BASE_URL=http://localhost:8080/v1
export DOCKET_TEXT_MODEL=model
```

LM Studio serves at `http://localhost:1234/v1` the same way.

`DOCKET_LLM_API_KEY` is sent when set; a local server usually needs none. A
server on `localhost`, `127.0.0.1` or `::1` keeps the long local timeouts; any
other host is treated as hosted: it gets `DOCKET_LLM_HOSTED_TIMEOUT_S` and is
retried on timeouts.

Without a vision model, turn the fallback off with `DOCKET_OCR_FALLBACKS=`
(empty). Scans are then read by Tesseract alone and never re-read.

## Choosing a model

Docket has not been measured across model sizes, so there is no tested
minimum. What the pipeline asks of a model:

- Instruction following into a JSON object that matches a Pydantic schema,
  with a citation (the quoted line) for every field.
- A context window that fits `DOCKET_EXTRACT_CHUNK_CHARS` (12,000 characters by
  default) plus the schema; longer documents are chunked and merged.
- For vision: a multimodal model that transcribes digits exactly. It can
  still change digits to make totals add up, which is why its reading is
  checked against the OCR reading it overruled.

Try yours on your own documents: `docket batch` reports `needs_review` rates,
and the review reasons say what failed.

## One backend per call, in Python

Settings in the environment are process-wide. To use different models in one
process, pass a backend:

```python
from docket import OpenAICompatibleBackend, ProcessOptions, process_document

gpu = OpenAICompatibleBackend(base_url="http://gpu-box:8000/v1", text_model="<model>", vision_model="")
result = process_document("scan.pdf", ProcessOptions(llm=gpu, ocr={"fallbacks": []}))
```

Arguments left out are read from the `DOCKET_*` settings at call time.
`OllamaBackend(host=..., text_model=..., vision_model=..., think=...)` works
the same way.

## Your own backend

Anything with these four members is an `LLMBackend`: an SDK client, a
gateway, a model loaded in-process.

```python
from docket import LLMReply, ProcessOptions, process_document


class MyBackend:
    text_model = "my-model"   # names the model in metrics and traces
    vision_model = ""         # empty: the vlm OCR backend is unavailable

    def generate_json(self, prompt: str, *, schema: dict | None, timeout: float) -> LLMReply:
        text = my_client.complete(prompt, json_schema=schema, timeout=timeout)
        return LLMReply(text)  # input_tokens / output_tokens if you have them

    def transcribe_image(self, image: bytes, *, mime: str, prompt: str, timeout: float) -> LLMReply:
        raise NotImplementedError


result = process_document("invoice.pdf", ProcessOptions(llm=MyBackend(), ocr={"fallbacks": []}))
```

Raise `docket.LLMError` on failure; any other exception is wrapped into one,
so a failing call becomes a failed attempt, not a crash. Docket still applies
`DOCKET_LLM_CONCURRENCY`, counts usage per document and traces to Langfuse
when configured; retries and timeouts are the backend's own.
