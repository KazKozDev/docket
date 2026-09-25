from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("SWARMS_TELEMETRY_ON", "false")

BASE_URL = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
API_KEY = os.getenv("OPENAI_API_KEY", "local")
ORCH_MODEL = os.getenv("ORCH_MODEL", "").strip()
WORKER_MODEL = os.getenv("WORKER_MODEL", "").strip()
MAX_LOOPS = int(os.getenv("MAX_LOOPS", "2"))

# Чтобы LiteLLM внутри swarms тоже бил в роутер, а не в api.openai.com
os.environ["OPENAI_BASE_URL"] = BASE_URL
os.environ["OPENAI_API_BASE"] = BASE_URL
os.environ["OPENAI_API_KEY"] = API_KEY


def list_router_models() -> list[str]:
    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=30)
    try:
        return sorted({item.id for item in client.models.list().data if getattr(item, "id", None)})
    except Exception as exc:
        raise RuntimeError(f"Роутер {BASE_URL} не ответил на GET /models: {exc}") from exc


def resolve_models(available: list[str], orch: str | None = None, worker: str | None = None) -> tuple[str, str]:
    fallback = available[0] if available else "default"
    orch_model = orch or ORCH_MODEL or fallback
    worker_model = worker or WORKER_MODEL or orch_model
    return orch_model, worker_model
