"""Download the validation artifacts Docket does not ship.

    from docket import fetch_einvoice_resources
    fetch_einvoice_resources()          # every artifact not in the package

Some official artifacts (Peppol BIS Billing, the Factur-X schemas and
Schematron, the UN/CEFACT CII D16B schemas) are published for implementers
without redistribution terms Docket could verify, so the package does not
include them (docs/THIRD_PARTY_LICENSES.md). This downloads them from their
upstream release, only when called, into the download root
(`DOCKET_EINVOICE_DOWNLOADS`, default `~/.cache/docket/einvoice`). Every
archive is checked against its pinned SHA-256 and every extracted file
against the manifest before it is put in place. Their upstream terms apply.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from . import artifacts, sources


def downloadable() -> list[str]:
    """The ids of the artifacts that are downloaded rather than shipped."""
    return [e["id"] for e in artifacts.manifest()["artifacts"] if not artifacts.redistributable(e) and e["files"]]


def fetch_einvoice_resources(artifact_ids: Iterable[str] | None = None, *, force: bool = False) -> list[str]:
    """Download the given artifacts (default: every one not shipped) into
    the download root. Already present, intact artifacts are skipped unless
    `force`. Returns the ids that were downloaded.

    Needs network access and, for the Peppol rules (compiled from the
    published Schematron), the [einvoice] extra."""
    from .validator import _require

    wanted = list(artifact_ids) if artifact_ids is not None else downloadable()
    by_id = {source["id"]: source for source in sources.SOURCES}
    dest = artifacts.download_root()
    fetched = []
    with tempfile.TemporaryDirectory(prefix="docket-einvoice-") as tmp:
        cache = Path(tmp) / "archives"
        for artifact_id in wanted:
            entry = artifacts.artifact(artifact_id)
            if artifacts.redistributable(entry):
                continue  # shipped with the package
            if not force and not artifacts.missing([artifact_id]) and not _damaged(entry):
                continue
            source = by_id.get(artifact_id)
            if source is None or source["sha256"] != entry["sha256"]:
                raise artifacts.ArtifactError(f"no pinned source for {artifact_id!r} matches the manifest")
            if source.get("compile"):
                _require()
            staging = Path(tmp) / artifact_id
            try:
                written = sources.install(source, staging, cache)
            except sources.SourceError as exc:
                raise artifacts.ArtifactError(str(exc)) from exc
            produced = {str(p.relative_to(staging)) for p in written}
            if produced != set(entry["files"]):
                raise artifacts.ArtifactError(f"{artifact_id}: the source produced other files than the manifest lists")
            for relative, expected in entry["files"].items():
                if expected is not None and _sha256(staging / relative) != expected:
                    raise artifacts.ArtifactError(f"{artifact_id}: {relative} does not match the manifest")
            for relative in entry["files"]:
                target = dest / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staging / relative, target)
            fetched.append(artifact_id)
    return fetched


def _damaged(entry: dict) -> bool:
    return any(
        expected is not None and _sha256(artifacts.path(relative)) != expected
        for relative, expected in entry["files"].items()
    )


def _sha256(target: Path) -> str:
    return hashlib.sha256(target.read_bytes()).hexdigest()


__all__ = ["downloadable", "fetch_einvoice_resources"]
