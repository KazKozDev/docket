"""The vendored official validation artifacts and their manifest.

Files live in `resources/` next to this module (or in
`DOCKET_EINVOICE_RESOURCES`, for a deployment that keeps its own copy);
`resources/manifest.json` records each file's SHA-256 with the source,
version and license it came from. `scripts/update_einvoice_resources.py`
rebuilds both from the pinned official downloads.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from .. import config


class ArtifactError(RuntimeError):
    """A validation artifact is missing or does not match the manifest."""


def root() -> Path:
    override = getattr(config, "EINVOICE_RESOURCES", None)
    return Path(override) if override else Path(__file__).parent / "resources"


@lru_cache(maxsize=4)
def _manifest(path: str) -> dict:
    manifest = Path(path) / "manifest.json"
    if not manifest.exists():
        raise ArtifactError(f"no validation artifacts at {Path(path)} (manifest.json missing)")
    return json.loads(manifest.read_text(encoding="utf-8"))


def manifest() -> dict:
    return _manifest(str(root()))


def artifact(artifact_id: str) -> dict:
    for entry in manifest()["artifacts"]:
        if entry["id"] == artifact_id:
            return entry
    raise ArtifactError(f"unknown artifact {artifact_id!r}")


def path(relative: str) -> Path:
    target = root() / relative
    if not target.exists():
        raise ArtifactError(f"validation artifact {relative} is missing from {root()}")
    return target


def verify() -> list[str]:
    """Every file the manifest lists, checked against its SHA-256."""
    problems = []
    for entry in manifest()["artifacts"]:
        for relative, expected in entry["files"].items():
            target = root() / relative
            if not target.exists():
                problems.append(f"missing: {relative} ({entry['id']})")
            elif hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                problems.append(f"checksum mismatch: {relative} ({entry['id']})")
    return problems


__all__ = ["ArtifactError", "artifact", "manifest", "path", "root", "verify"]
