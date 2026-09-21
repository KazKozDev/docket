"""Write the HTTP API's OpenAPI spec to docs/openapi.json.

Client generators (openapi-generator, orval, openapi-typescript, …) can use
this file without running the service. CI fails if it drifts from the code.
"""
import json
from pathlib import Path

from docket.api import app

out = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
out.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n")
print(f"wrote {out}")
