"""The official validation artifacts and their manifest.

`resources/manifest.json` records every artifact's files with their SHA-256,
source, version and license (null for XSLT compiled locally from a hashed
Schematron source). Files are looked up in two places:

- the resource root: `resources/` next to this module, or
  `DOCKET_EINVOICE_RESOURCES` for a deployment that keeps its own copy;
- the download root (`DOCKET_EINVOICE_DOWNLOADS`, default
  `~/.cache/docket/einvoice`), where `docket einvoice fetch` puts the
  artifacts Docket does not ship because their redistribution terms are
  unverified (`redistributable: false`; see docs/THIRD_PARTY_LICENSES.md).

`scripts/update_einvoice_resources.py` rebuilds the resources and the
manifest from the pinned official downloads.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from .. import config

FETCH_HINT = "run `docket einvoice fetch` (or `docket.fetch_einvoice_resources()`) to download them"


class ArtifactError(RuntimeError):
    """A validation artifact is missing or does not match the manifest."""


def root() -> Path:
    override = getattr(config, "EINVOICE_RESOURCES", None)
    return Path(override) if override else Path(__file__).parent / "resources"


def download_root() -> Path:
    override = getattr(config, "EINVOICE_DOWNLOADS", None)
    if override:
        return Path(override)
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "docket" / "einvoice"


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


def redistributable(entry: dict) -> bool:
    return entry.get("redistributable", True)


def _locate(relative: str) -> Path | None:
    for base in (root(), download_root()):
        target = base / relative
        if target.exists():
            return target
    return None


def path(relative: str) -> Path:
    target = _locate(relative)
    if target is None:
        raise ArtifactError(f"validation artifact {relative} is missing from {root()} and {download_root()}")
    return target


def missing(artifact_ids) -> list[str]:
    """The ids among `artifact_ids` with a file present in neither root."""
    return [
        artifact_id for artifact_id in dict.fromkeys(artifact_ids)
        if any(_locate(relative) is None for relative in artifact(artifact_id)["files"])
    ]


def verify() -> list[str]:
    """Every file the manifest lists, checked against its SHA-256. Artifacts
    that are downloaded on request are only checked when present."""
    problems = []
    for entry in manifest()["artifacts"]:
        for relative, expected in entry["files"].items():
            target = _locate(relative)
            if target is None:
                if redistributable(entry):
                    problems.append(f"missing: {relative} ({entry['id']})")
            elif expected is not None and hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                problems.append(f"checksum mismatch: {relative} ({entry['id']})")
    return problems


__all__ = [
    "FETCH_HINT", "ArtifactError", "artifact", "download_root", "manifest", "missing", "path",
    "redistributable", "root", "verify",
]
