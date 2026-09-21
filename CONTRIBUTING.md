# Contributing to docket

Thanks for helping. Bug reports, new document types, and new export formats
are all welcome.

## Setup

```bash
git clone https://github.com/KazKozDev/docket.git
cd docket && python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tesseract must be on PATH. No test needs a running LLM: mock `llm_client`
calls with `monkeypatch`, as the existing tests do.

## Pull requests

- One change per PR, with a test that fails without it.
- Keep `validate.py` deterministic: it must never call a model.
- If you touch the HTTP API, regenerate the spec: `python scripts/export_openapi.py`
  (CI fails when `docs/openapi.json` is stale).
- Add a line under `[Unreleased]` in `CHANGELOG.md` for anything user-visible.
- Do not add dependencies under copyleft licenses (GPL, AGPL, LGPL) to the
  runtime `dependencies`; docket must stay embeddable in closed-source apps.
  Test-only tools in the `dev` extra are fine.

## Adding a document type

Built-in types live in `schemas.py` (schema), `classify.py` (keyword rules),
`validate.py` (validator) and `doctypes.py` (LLM description). Types specific
to one business should be a plugin registered through
`register_document_type` instead; see `examples/custom_document_type.py`.

## Adding an export format

Implement `func(document) -> str | dict` in `src/docket/export/`, register it
at the bottom of `src/docket/export/__init__.py`, and add a test that parses
the output. For formats that don't belong in core (a single company's ERP,
say), publish a plugin package instead; see `examples/exporter_plugin/`.

## Releasing (maintainers)

1. Bump `__version__` in `src/docket/__init__.py` and move `[Unreleased]` in
   `CHANGELOG.md` under the new version.
2. Commit, then `git tag vX.Y.Z && git push --tags`.
3. The `Release` workflow publishes to PyPI and GHCR and creates the GitHub release.

One-time setup: on PyPI, add a Trusted Publisher for this repository with
workflow `release.yml` and environment `pypi`; in GitHub, create the `pypi`
environment under Settings → Environments.

## License

By contributing you agree that your contributions are licensed under the
Apache License 2.0.
