FROM python:3.12-slim

LABEL org.opencontainers.image.title="docket" \
      org.opencontainers.image.description="Invoice and receipt extraction API: OCR + LLM, validated JSON, EU e-invoicing export" \
      org.opencontainers.image.source="https://github.com/KazKozDev/docket" \
      org.opencontainers.image.licenses="Apache-2.0"

# tesseract-ocr for the OCR tier, with the main EU language packs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-fra \
    tesseract-ocr-spa \
    tesseract-ocr-ita \
    tesseract-ocr-nld \
    tesseract-ocr-pol \
    tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src/ src/
RUN pip install --no-cache-dir ".[api]"

RUN useradd --create-home --uid 1000 docket && mkdir -p /app/data && chown docket /app/data
USER docket

# Point at Ollama on the host by default; for an OpenAI-compatible provider set
# DOCKET_LLM_PROVIDER=openai, DOCKET_LLM_BASE_URL and DOCKET_LLM_API_KEY.
ENV OLLAMA_HOST=http://host.docker.internal:11434 \
    PYTHONUNBUFFERED=1

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["docket-api", "--host", "0.0.0.0", "--port", "8000"]
