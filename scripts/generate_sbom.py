"""Generate a CycloneDX SBOM for package dependencies and vendored artefacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()


def _component(requirement: str, scope: str, extra: str | None = None) -> dict:
    name = _name(requirement)
    try:
        installed = version(name)
    except PackageNotFoundError:
        installed = "unresolved"
    properties = [{"name": "docket:dependency-scope", "value": scope}]
    if extra:
        properties.append({"name": "docket:extra", "value": extra})
    return {
        "type": "library",
        "name": name,
        "version": installed,
        "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{installed}",
        "properties": properties,
    }


def build() -> dict:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / "src/docket/einvoice/resources/manifest.json").read_text())
    components = [_component(req, "runtime") for req in project["dependencies"]]
    for extra, requirements in project.get("optional-dependencies", {}).items():
        if extra == "all":
            continue
        components.extend(_component(req, "optional", extra) for req in requirements)
    for artifact in manifest["artifacts"]:
        components.append({
            "type": "data",
            "name": artifact["name"],
            "version": artifact["version"],
            "bom-ref": f"docket-artifact:{artifact['id']}@{artifact['version']}",
            "hashes": [{"alg": "SHA-256", "content": artifact["sha256"]}],
            "licenses": [{"license": {"name": artifact["license"]}}],
            "externalReferences": [{"type": "distribution", "url": artifact["url"]}],
            "properties": [
                {"name": "docket:dependency-scope",
                 "value": "bundled-artifact" if artifact.get("redistributable", True) else "downloaded-on-request"},
                {"name": "docket:artifact-id", "value": artifact["id"]},
            ],
        })
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "library", "name": project["name"], "version": version("docket-idp")}},
        "components": components,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
