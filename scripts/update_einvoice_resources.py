"""Rebuild the vendored e-invoice validation artifacts from their official sources.

    python scripts/update_einvoice_resources.py            # resources + test fixtures
    python scripts/update_einvoice_resources.py --check    # verify the vendored files only
    python scripts/update_einvoice_resources.py --fixtures-only  # test fixtures, e.g. Peppol's, only

Every source is pinned by URL and SHA-256; a download whose hash differs is
refused. The script extracts only the files validation needs into
src/docket/einvoice/resources/, compiles the Peppol Schematron (published
as .sch only) to XSLT with SchXslt under SaxonC-HE, and writes
resources/manifest.json with each file's SHA-256, version, source and
license. To move to a new release, change the pinned URL and hash in
src/docket/einvoice/sources.py, rerun, run the test suite, and commit the diff.

Artifacts marked `redistributable: False` are written too, so the full
test suite can run, but they are git-ignored and excluded from the wheel;
users download them with `docket einvoice fetch`.

Needs network access and the [einvoice] extra (saxonche).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "src" / "docket" / "einvoice" / "resources"
FIXTURES = ROOT / "tests" / "fixtures" / "einvoice" / "official"

sys.path.insert(0, str(ROOT / "src"))
from docket.einvoice.sources import (
    SCHXSLT,
    SOURCES,
    SourceError,
    install,
    sha256,
)


def build(cache: Path) -> None:
    license_texts = {
        path.name: path.read_bytes()
        for path in (RESOURCES / "licenses").glob("*.txt")
    }
    shutil.rmtree(RESOURCES, ignore_errors=True)
    shutil.rmtree(FIXTURES, ignore_errors=True)
    RESOURCES.mkdir(parents=True)
    licenses = RESOURCES / "licenses"
    licenses.mkdir()
    for name, contents in license_texts.items():
        (licenses / name).write_bytes(contents)
    manifest = {"generated_by": "scripts/update_einvoice_resources.py", "compiler": _public(SCHXSLT), "artifacts": []}
    for source in SOURCES:
        print(f"{source['id']}: {source['url']}")
        try:
            files = install(source, RESOURCES, cache, FIXTURES)
        except SourceError as exc:
            raise SystemExit(str(exc)) from exc
        entry = _public(source)
        # Compiled output depends on the local SaxonC; it is vouched for by
        # its pinned .sch source, so it records no hash (null).
        compiled = {str(Path(sch).with_suffix(".xsl")) for sch in source.get("compile", [])}
        entry["files"] = {
            str(f.relative_to(RESOURCES)): None if str(f.relative_to(RESOURCES)) in compiled else sha256(f.read_bytes())
            for f in sorted(files)
        }
        manifest["artifacts"].append(entry)
    (RESOURCES / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    total = sum(f.stat().st_size for f in RESOURCES.rglob("*") if f.is_file())
    print(f"wrote {RESOURCES} ({total / 1e6:.1f} MB) and fixtures under {FIXTURES}")


def fixtures_only(cache: Path) -> None:
    """Extract the test fixtures, leaving the resources and manifest alone."""
    import io
    import zipfile

    from docket.einvoice.sources import download, extract

    for source in SOURCES:
        if source.get("fixtures"):
            try:
                with zipfile.ZipFile(io.BytesIO(download(source, cache))) as archive:
                    extract(archive, source["fixtures"], FIXTURES)
            except SourceError as exc:
                raise SystemExit(str(exc)) from exc
    print(f"wrote fixtures under {FIXTURES}")


def _public(source: dict) -> dict:
    return {
        k: source[k]
        for k in ("id", "name", "version", "url", "sha256", "license", "license_files", "redistributable")
        if k in source
    }


def check() -> int:
    from docket.einvoice.artifacts import verify

    problems = verify()
    for problem in problems:
        print(problem)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only verify vendored files against the manifest")
    parser.add_argument("--fixtures-only", action="store_true", help="only extract the test fixtures")
    parser.add_argument("--cache", default=str(ROOT / ".cache" / "einvoice-sources"), help="download cache")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(check())
    if args.fixtures_only:
        fixtures_only(Path(args.cache))
        return
    build(Path(args.cache))


if __name__ == "__main__":
    main()
