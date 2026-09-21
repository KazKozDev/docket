"""Rebuild the vendored e-invoice validation artifacts from their official sources.

    python scripts/update_einvoice_resources.py            # resources + test fixtures
    python scripts/update_einvoice_resources.py --check    # verify the vendored files only

Every source is pinned by URL and SHA-256; a download whose hash differs is
refused. The script extracts only the files validation needs into
src/docket/einvoice/resources/, compiles the Peppol Schematron (published
as .sch only) to XSLT with SchXslt under SaxonC-HE, and writes
resources/manifest.json with each file's SHA-256, version, source and
license. To move to a new release, change the pinned URL and hash below,
rerun, run the test suite, and commit the diff.

Needs network access and the [einvoice] extra (saxonche).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "src" / "docket" / "einvoice" / "resources"
FIXTURES = ROOT / "tests" / "fixtures" / "einvoice" / "official"

SOURCES = [
    {
        "id": "kosit-configuration",
        "name": "KoSIT validator configuration for XRechnung (UBL 2.1 and UN/CEFACT CII D16B XML Schemas)",
        "version": "XRechnung 3.0.2, release 2026-08-31",
        "url": "https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/download/v2026-08-31/xrechnung-3.0.2-validator-configuration-2026-08-31.zip",
        "sha256": "2530cd107c414511c5d0462ec10f886910395abfca820db82e83d70bf01221a8",
        "license": "Apache-2.0 (configuration); bundled OASIS UBL 2.1 schemas under the OASIS IPR Policy; UN/CEFACT D16B schemas published by UNECE for free use",
        "files": {
            "resources/ubl/2.1/xsd/common/*.xsd": "ubl-2.1/xsd/common/",
            "resources/ubl/2.1/xsd/maindoc/UBL-Invoice-2.1.xsd": "ubl-2.1/xsd/maindoc/",
            "resources/ubl/2.1/xsd/maindoc/UBL-CreditNote-2.1.xsd": "ubl-2.1/xsd/maindoc/",
            "resources/cii/16b/xsd/*.xsd": "cii-d16b/xsd/",
        },
    },
    {
        "id": "en16931-ubl",
        "name": "CEN EN 16931 validation artefacts, UBL syntax",
        "version": "1.3.16",
        "url": "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/validation-1.3.16/en16931-ubl-1.3.16.zip",
        "sha256": "bafada015efbc5248bf5e05ad2191e1d9833ef96e9dd5f4bce420a747342da85",
        "license": "EUPL-1.2",
        "files": {"xslt/EN16931-UBL-validation.xslt": "en16931/"},
        "fixtures": {"examples/*.xml": "en16931-ubl/", "examples/*.XML": "en16931-ubl/"},
    },
    {
        "id": "en16931-cii",
        "name": "CEN EN 16931 validation artefacts, UN/CEFACT CII syntax",
        "version": "1.3.16",
        "url": "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/validation-1.3.16/en16931-cii-1.3.16.zip",
        "sha256": "1cd53cb8a84d38aedc82c0caede217da983a7934dd663f793a092fd66443c561",
        "license": "EUPL-1.2",
        "files": {"xslt/EN16931-CII-validation.xslt": "en16931/"},
        "fixtures": {"examples/*.xml": "en16931-cii/"},
    },
    {
        "id": "xrechnung-schematron",
        "name": "KoSIT Schematron rules for CIUS XRechnung",
        "version": "XRechnung 3.0.2, schematron 2.6.0",
        "url": "https://github.com/itplr-kosit/xrechnung-schematron/releases/download/v2.6.0/xrechnung-3.0.2-schematron-2.6.0.zip",
        "sha256": "ca5e07afd04e72cd283d581590ffff3a15f4aac9d2f40c993345d78e67ea22b4",
        "license": "Apache-2.0",
        "files": {
            "schematron/ubl/XRechnung-UBL-validation.xsl": "xrechnung/",
            "schematron/cii/XRechnung-CII-validation.xsl": "xrechnung/",
            "LICENSE": "xrechnung/",
        },
    },
    {
        "id": "xrechnung-testsuite",
        "name": "KoSIT XRechnung test suite (test fixtures only, not shipped)",
        "version": "XRechnung 3.0.2, release 2026-08-31",
        "url": "https://github.com/itplr-kosit/xrechnung-testsuite/releases/download/v2026-08-31/xrechnung-3.0.2-testsuite-2026-08-31.zip",
        "sha256": "1e81ef7563e0fa04c0e0b0631318a7e0843a3c0beb57d83ef95ac0c4a095e3d7",
        "license": "Apache-2.0",
        "files": {},
        "fixtures": {
            "instances/standard/01.01a-INVOICE_ubl.xml": "xrechnung/",
            "instances/standard/01.01a-INVOICE_uncefact.xml": "xrechnung/",
            "instances/standard/02.01a-INVOICE_ubl.xml": "xrechnung/",
            "instances/standard/02.01a-INVOICE_uncefact.xml": "xrechnung/",
        },
    },
    {
        "id": "peppol-bis-billing",
        "name": "OpenPeppol BIS Billing 3.0 Schematron",
        "version": "3.0.20",
        "url": "https://github.com/OpenPEPPOL/peppol-bis-invoice-3/archive/refs/tags/v3.0.20.zip",
        "sha256": "4c43040f0654abd789bb0c9a2ffbb9e795acd94f20282af8ca1db3dda832f656",
        "license": "no license file in the repository; published by OpenPeppol for implementers of Peppol BIS Billing 3.0",
        "files": {
            "peppol-bis-invoice-3-3.0.20/rules/sch/PEPPOL-EN16931-UBL.sch": "peppol/",
            "peppol-bis-invoice-3-3.0.20/rules/sch/PEPPOL-EN16931-CII.sch": "peppol/",
        },
        "compile": ["peppol/PEPPOL-EN16931-UBL.sch", "peppol/PEPPOL-EN16931-CII.sch"],
        "fixtures": {
            "peppol-bis-invoice-3-3.0.20/rules/examples/*.xml": "peppol/examples/",
            "peppol-bis-invoice-3-3.0.20/rules/unit-UBL-PEPPOL/*.xml": "peppol/unit-UBL-PEPPOL/",
        },
    },
    {
        "id": "factur-x",
        "name": "Factur-X 1.09 / ZUGFeRD XML Schemas and Schematron per profile (FNFE-MPE and FeRD), as redistributed in the factur-x 6.8 Python package",
        "version": "Factur-X 1.09 (factur-x 6.8)",
        "url": "https://files.pythonhosted.org/packages/37/be/9a9020187e4805d61668b5d8c4c263731b3697f9eba2aa6a689d3e7eea4b/factur_x-6.8-py3-none-any.whl",
        "sha256": "02b57dd57f59d0cdd87f034a538bf30a4b9241a72a19cd75fe1538233163ee15",
        "license": "artefacts published free of charge by FNFE-MPE / FeRD (official download is form-gated, no license file); the factur-x package that redistributes them is BSD-2-Clause",
        "files": {
            f"facturx/xsd_and_schematron/facturx-{profile}/*": f"factur-x/{profile}/"
            for profile in ("minimum", "basicwl", "basic", "en16931", "extended")
        },
    },
]

SCHXSLT = {
    "id": "schxslt",
    "name": "SchXslt Schematron-to-XSLT compiler (used to compile the Peppol rules; not shipped)",
    "version": "1.10.1",
    "url": "https://repo1.maven.org/maven2/name/dmaus/schxslt/schxslt/1.10.1/schxslt-1.10.1.jar",
    "sha256": "4f4f21edab7b37f96ad59ae12a344d3510f1092ac46b6d81a4efa0120b73cb58",
    "license": "MIT",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(source: dict, cache: Path) -> bytes:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / Path(source["url"]).name
    if not target.exists():
        print(f"downloading {source['url']}")
        with urllib.request.urlopen(source["url"], timeout=120) as response:
            target.write_bytes(response.read())
    data = target.read_bytes()
    digest = sha256(data)
    if digest != source["sha256"]:
        raise SystemExit(f"{source['id']}: sha256 {digest} does not match the pinned {source['sha256']}")
    return data


def extract(archive: zipfile.ZipFile, mapping: dict[str, str], dest_root: Path) -> list[Path]:
    written = []
    names = archive.namelist()
    for pattern, dest in mapping.items():
        matches = [n for n in names if fnmatch.fnmatch(n, pattern) and not n.endswith("/")]
        if not matches:
            raise SystemExit(f"nothing matches {pattern!r}")
        for name in matches:
            target = dest_root / dest / Path(name).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            written.append(target)
    return written


def compile_schematron(sch: Path, schxslt_jar: bytes) -> Path:
    from saxonche import PySaxonProcessor

    work = RESOURCES.parent / ".schxslt"
    shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(schxslt_jar)) as jar:
        jar.extractall(work, members=[n for n in jar.namelist() if n.startswith("xslt/")])
    try:
        with PySaxonProcessor(license=False) as processor:
            xslt = processor.new_xslt30_processor()
            compiler = xslt.compile_stylesheet(stylesheet_file=str(work / "xslt/2.0/pipeline-for-svrl.xsl"))
            output = compiler.transform_to_string(source_file=str(sch))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    target = sch.with_suffix(".xsl")
    target.write_text(output, encoding="utf-8")
    return target


def build(cache: Path) -> None:
    shutil.rmtree(RESOURCES, ignore_errors=True)
    shutil.rmtree(FIXTURES, ignore_errors=True)
    RESOURCES.mkdir(parents=True)
    schxslt_jar = fetch(SCHXSLT, cache)
    manifest = {"generated_by": "scripts/update_einvoice_resources.py", "compiler": _public(SCHXSLT), "artifacts": []}
    for source in SOURCES:
        data = fetch(source, cache)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = extract(archive, source["files"], RESOURCES)
            extract(archive, source.get("fixtures", {}), FIXTURES)
        for sch in source.get("compile", []):
            files.append(compile_schematron(RESOURCES / sch, schxslt_jar))
        entry = _public(source)
        entry["files"] = {str(f.relative_to(RESOURCES)): sha256(f.read_bytes()) for f in sorted(files)}
        manifest["artifacts"].append(entry)
    (RESOURCES / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    total = sum(f.stat().st_size for f in RESOURCES.rglob("*") if f.is_file())
    print(f"wrote {RESOURCES} ({total / 1e6:.1f} MB) and fixtures under {FIXTURES}")


def _public(source: dict) -> dict:
    return {k: source[k] for k in ("id", "name", "version", "url", "sha256", "license")}


def check() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from docket.einvoice.artifacts import verify

    problems = verify()
    for problem in problems:
        print(problem)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only verify vendored files against the manifest")
    parser.add_argument("--cache", default=str(ROOT / ".cache" / "einvoice-sources"), help="download cache")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(check())
    build(Path(args.cache))


if __name__ == "__main__":
    main()
